//! The tmux side: which server to talk to, the ONE snapshot call per refresh,
//! and its parse into typed rows. Mirrors agent-roster.py `FMT` / `FIELDS` /
//! `CLIENT_FMT` / `snapshot` / `parse_windows` / `parse_clients`.
//!
//! # Which server
//! By default plain `tmux`, which (like every script here) means `$TMUX`'s
//! socket inside tmux and the default socket outside it. `AGENT_UI_TMUX_SOCKET`
//! (or `agent-ui --socket`) overrides it: a value containing `/` is a socket
//! PATH (`tmux -S`), anything else a socket NAME (`tmux -L`). The shell
//! scripts this crate calls (agent-jump.sh, stash.sh via run-shell, ...) know
//! nothing of that variable, so [`Tmux::script_env`] exports `TMUX=<socket
//! path>,0,0` to them: plain `tmux` honours `$TMUX` when given no -L/-S, which
//! is how a test points agent-jump.sh at a private `tmux -L` server.

use crate::proc::{self, Output, RunError};
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

/// The unit separator every multi-field format uses (agent-roster.py `US`).
pub const US: char = '\x1f';
/// The env var naming a non-default tmux server (name, or path with a `/`).
pub const SOCKET_ENV: &str = "AGENT_UI_TMUX_SOCKET";
/// agent-roster.py `tmux()`: every tmux call gives up after 5 s.
pub const TMUX_TIMEOUT: Duration = Duration::from_secs(5);

/// A tmux server other than the ambient one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Socket {
    /// `tmux -L <name>`
    Name(String),
    /// `tmux -S <path>`
    Path(String),
}

impl Socket {
    /// Parse an `AGENT_UI_TMUX_SOCKET` value; "" means none.
    pub fn parse(v: &str) -> Option<Socket> {
        if v.is_empty() {
            None
        } else if v.contains('/') {
            Some(Socket::Path(v.to_string()))
        } else {
            Some(Socket::Name(v.to_string()))
        }
    }

    /// The socket's filesystem path, computed the way tmux does for `-L`:
    /// `realpath(${TMUX_TMPDIR:-/tmp})/tmux-<uid>/<name>`.
    pub fn path(&self) -> PathBuf {
        match self {
            Socket::Path(p) => PathBuf::from(p),
            Socket::Name(n) => {
                let base = std::env::var("TMUX_TMPDIR").ok().filter(|s| !s.is_empty()).unwrap_or_else(|| "/tmp".into());
                let base = std::fs::canonicalize(&base).unwrap_or_else(|_| PathBuf::from(&base));
                base.join(format!("tmux-{}", uid())).join(n)
            }
        }
    }
}

/// The env changes that give a child a UTF-8 LC_CTYPE without touching its
/// collation: empty when the effective LC_CTYPE (LC_ALL > LC_CTYPE > LANG)
/// already names UTF-8.
///
/// Why: a tmux client with no `$TMUX` and a non-UTF-8 locale prints US as
/// `_`, so agent-jump.sh's `client_info` and `list` (both US-split by awk)
/// see nothing, e.g. from the WezTerm sidebar under LC_ALL=C. We cannot
/// pass `-u` to the script's own tmux calls, so we fix its locale instead.
/// LC_ALL would override LC_CTYPE, so a non-UTF-8 LC_ALL is unset and its
/// value kept for LC_COLLATE and LC_NUMERIC: the script's `sort` must
/// still order like our in-process [`crate::model::needs_order`], which
/// collates by the inherited locale.
pub fn utf8_ctype_env(get: impl Fn(&str) -> Option<String>) -> Vec<(&'static str, Option<String>)> {
    let set = |k: &str| get(k).filter(|v| !v.is_empty());
    let is_utf8 = |v: &str| {
        let v = v.to_ascii_lowercase();
        v.contains("utf-8") || v.contains("utf8")
    };
    let effective = set("LC_ALL").or_else(|| set("LC_CTYPE")).or_else(|| set("LANG")).unwrap_or_else(|| "C".into());
    if is_utf8(&effective) {
        return Vec::new();
    }
    let mut out = Vec::new();
    if let Some(all) = set("LC_ALL") {
        out.push(("LC_ALL", None));
        out.push(("LC_COLLATE", Some(all.clone())));
        out.push(("LC_NUMERIC", Some(all)));
    }
    out.push(("LC_CTYPE", Some("en_US.UTF-8".into())));
    out
}

/// The real uid (`getuid(2)`).
pub fn uid() -> u32 {
    extern "C" {
        fn getuid() -> u32;
    }
    // SAFETY: getuid has no preconditions and cannot fail.
    unsafe { getuid() }
}

/// A handle on one tmux server. Cheap to clone.
#[derive(Debug, Clone, Default)]
pub struct Tmux {
    pub socket: Option<Socket>,
}

impl Tmux {
    /// The server named by `AGENT_UI_TMUX_SOCKET`, else the ambient one.
    pub fn from_env() -> Tmux {
        Tmux { socket: std::env::var(SOCKET_ENV).ok().as_deref().and_then(Socket::parse) }
    }

    pub fn with_socket(socket: Option<Socket>) -> Tmux {
        Tmux { socket }
    }

    /// `tmux -u [-L name | -S path]`.
    ///
    /// `-u` matters outside tmux (the WezTerm sidebar): a tmux client whose
    /// locale is not UTF-8 and that has no `$TMUX` rewrites every non-ASCII
    /// byte AND the US separator in command output as `_`, which would drop
    /// every snapshot row. Inside tmux ($TMUX set) it is a no-op.
    pub fn command(&self) -> Command {
        let mut c = Command::new("tmux");
        c.arg("-u");
        match &self.socket {
            Some(Socket::Name(n)) => {
                c.args(["-L", n]);
            }
            Some(Socket::Path(p)) => {
                c.args(["-S", p]);
            }
            None => {}
        }
        c
    }

    /// Prepare the environment of a script that calls plain `tmux`
    /// (agent-jump.sh): point it at this server (see module docs), and give
    /// it a UTF-8 LC_CTYPE when the inherited one is not ([`utf8_ctype_env`]).
    pub fn script_env(&self, cmd: &mut Command) {
        if let Some(s) = &self.socket {
            cmd.env("TMUX", format!("{},0,0", s.path().display()));
            cmd.env_remove("TMUX_PANE");
        }
        for (k, v) in utf8_ctype_env(|k| std::env::var(k).ok()) {
            match v {
                Some(v) => cmd.env(k, v),
                None => cmd.env_remove(k),
            };
        }
    }

    /// agent-roster.py `tmux(*args)`: run one tmux command line (may contain
    /// `;` separators) with the 5 s deadline.
    pub fn run<S: AsRef<std::ffi::OsStr>>(&self, args: &[S]) -> Result<Output, RunError> {
        let mut c = self.command();
        c.args(args);
        proc::run(c, None, TMUX_TIMEOUT)
    }

    /// agent-roster.py `run_bg`: hand a shell command to the tmux server
    /// (`run-shell -b`), so it outlives this process. Fire and forget.
    pub fn run_bg(&self, shell_cmd: &str) {
        let _ = self.run(&["run-shell", "-b", shell_cmd]);
    }

    /// agent-roster.py `snapshot`: every window and every client in ONE
    /// round trip → stdout, or "" when tmux failed (no server, timeout).
    pub fn snapshot_text(&self) -> String {
        match self.run(&["list-windows", "-a", "-F", &fmt(), ";", "list-clients", "-F", &client_fmt()]) {
            Ok(o) if o.ok() => o.stdout,
            _ => String::new(),
        }
    }

    /// [`snapshot_text`](Self::snapshot_text) parsed.
    pub fn snapshot(&self) -> Snapshot {
        Snapshot::parse(&self.snapshot_text())
    }
}

/// agent-roster.py `FIELDS`, plus `stash_origin` (stash.sh's `@stash_origin`,
/// the session a parked tab came from; the menu shows it).
pub const FIELDS: [&str; 21] = [
    "session", "index", "id", "name", "state", "summary", "workflow", "cua", "since", "last_attached",
    "blink", "stash_label", "stash_session", "stash_ts", "active", "panes", "path", "kind", "detail_kind",
    "detail", "stash_origin",
];

/// The tmux format of each FIELDS entry, in order.
pub const FORMATS: [&str; 21] = [
    "#{session_name}", "#{window_index}", "#{window_id}", "#{window_name}", "#{@agent_state}",
    "#{@agent_summary}", "#{@agent_workflow}", "#{@agent_cua}", "#{@agent_since}", "#{session_last_attached}",
    "#{@agent_blink}", "#{@stash_label}", "#{@stash_session}", "#{@stash_ts}", "#{window_active}",
    "#{window_panes}", "#{pane_current_path}", "#{@agent_kind}", "#{@agent_detail_kind}", "#{@agent_detail}",
    "#{@stash_origin}",
];

/// Stand-ins tmux writes for a newline / US inside a field value (Unicode
/// private-use code points), so free text can never split a row or add a
/// field. [`restore`] maps them back. A value that really contains one of
/// these code points is read as a newline / US; nothing writes them.
pub const LF_STANDIN: char = '\u{E00A}';
pub const US_STANDIN: char = '\u{E01F}';

/// Fields that are always tmux-generated digits / ids, never free text.
const PLAIN_FIELDS: [&str; 5] = ["#{window_index}", "#{window_id}", "#{session_last_attached}", "#{window_active}", "#{window_panes}"];

/// `#{name}` → `#{s/<LF>/<LF_STANDIN>/:#{s/<US>/<US_STANDIN>/:name}}`.
/// Verified on tmux 3.7: the `s/` modifier matches raw control bytes.
pub fn guarded(format: &str) -> String {
    if PLAIN_FIELDS.contains(&format) {
        return format.to_string();
    }
    let name = format.trim_start_matches("#{").trim_end_matches('}');
    format!("#{{s/\n/{LF_STANDIN}/:#{{s/{US}/{US_STANDIN}/:{name}}}}}")
}

/// One field as tmux printed it → its real value.
pub fn restore(field: &str) -> String {
    if field.contains([LF_STANDIN, US_STANDIN]) {
        field.replace(LF_STANDIN, "\n").replace(US_STANDIN, &US.to_string())
    } else {
        field.to_string()
    }
}

/// The window-row format: FORMATS joined by US, every free-text field
/// [`guarded`]. agent-roster.py's FMT is the unguarded first 20 fields; it
/// lost any window whose summary, label, detail or cwd held a newline or US,
/// while agent-jump.sh still listed it.
pub fn fmt() -> String {
    FORMATS.iter().map(|f| guarded(f)).collect::<Vec<_>>().join(&US.to_string())
}

/// Client rows ride in the same call. agent-roster.py `CLIENT_FMT` plus the
/// client's session: `"" US client US tty US window_id US session`. Starts
/// with an empty field and has 5 fields where a window row has 21, so the two
/// parsers can never take each other's rows.
pub const CLIENT_TAG: &str = "client";

pub fn client_fmt() -> String {
    ["", CLIENT_TAG, "#{client_tty}", "#{window_id}", &guarded("#{session_name}")].join(&US.to_string())
}

/// The holding session for parked tabs (agent-roster.py `HOLD`).
pub const HOLD: &str = "stash";

/// One `list-windows -a` row: one (session, window) pair. A window linked into
/// two sessions is two rows with the same `id`.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Window {
    pub session: String,
    /// window_index; 0 when tmux printed something unparseable (as Python).
    pub index: u32,
    /// window_id, `@N`.
    pub id: String,
    pub name: String,
    /// @agent_state: idle | running | needs-input | failed | done | "" (no agent).
    pub state: String,
    pub summary: String,
    /// @agent_workflow / @agent_cua: any non-empty value means set.
    pub workflow: String,
    pub cua: String,
    /// Raw @agent_since (`"<epoch> <state>"`).
    pub since: String,
    /// The epoch at the start of @agent_since, if it is all digits.
    pub since_t: Option<i64>,
    pub last_attached: i64,
    /// Raw @agent_blink (a global option, the same on every row).
    pub blink: String,
    pub stash_label: String,
    pub stash_session: String,
    pub stash_ts: String,
    pub stash_t: Option<i64>,
    pub stash_origin: String,
    /// window_active == "1".
    pub active: bool,
    /// window_panes; 1 when unparseable.
    pub panes: u32,
    /// pane_current_path of the window's active pane.
    pub path: String,
    /// @agent_kind: claude | codex | "".
    pub kind: String,
    /// @agent_detail_kind: perm | ask | fail | done | run | "".
    pub detail_kind: String,
    pub detail: String,
    /// agent-roster.py `label`: @stash_label (parked rows only), else the
    /// summary, else the window name.
    pub label: String,
}

fn digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

impl Window {
    /// One row → a Window, or None unless it has exactly FIELDS.len() fields.
    /// Stand-ins are [`restore`]d, so values hold their real newlines / US.
    pub fn parse(line: &str) -> Option<Window> {
        let owned: Vec<String> = line.split(US).map(restore).collect();
        if owned.len() != FIELDS.len() {
            return None;
        }
        let p: Vec<&str> = owned.iter().map(String::as_str).collect();
        let num = |s: &str| if digits(s) { s.parse::<i64>().ok() } else { None };
        let since_word = p[8].split(' ').next().unwrap_or("");
        let session = p[0].to_string();
        let parked = session == HOLD;
        let (summary, name, stash_label) = (p[5], p[3], p[11]);
        let label = if parked && !stash_label.is_empty() {
            stash_label
        } else if !summary.is_empty() {
            summary
        } else {
            name
        };
        Some(Window {
            index: num(p[1]).and_then(|n| u32::try_from(n).ok()).unwrap_or(0),
            id: p[2].into(),
            name: name.into(),
            state: p[4].into(),
            summary: summary.into(),
            workflow: p[6].into(),
            cua: p[7].into(),
            since: p[8].into(),
            since_t: num(since_word),
            last_attached: num(p[9]).unwrap_or(0),
            blink: p[10].into(),
            stash_label: stash_label.into(),
            stash_session: p[12].into(),
            stash_ts: p[13].into(),
            stash_t: num(p[13]),
            active: p[14] == "1",
            panes: num(p[15]).and_then(|n| u32::try_from(n).ok()).unwrap_or(1),
            path: p[16].into(),
            kind: p[17].into(),
            detail_kind: p[18].into(),
            detail: p[19].into(),
            stash_origin: p[20].into(),
            label: label.into(),
            session,
        })
    }

    pub fn is_parked(&self) -> bool {
        self.session == HOLD
    }
}

/// One `list-clients` row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Client {
    pub tty: String,
    pub window_id: String,
    /// "" when a 4-field (Python-format) row was parsed.
    pub session: String,
}

impl Client {
    /// agent-roster.py `parse_clients`, one row: `"" US client US tty US win [US session]`.
    pub fn parse(line: &str) -> Option<Client> {
        let p: Vec<&str> = line.split(US).collect();
        if (p.len() == 4 || p.len() == 5) && p[0].is_empty() && p[1] == CLIENT_TAG {
            Some(Client { tty: p[2].into(), window_id: p[3].into(), session: restore(p.get(4).unwrap_or(&"")) })
        } else {
            None
        }
    }
}

/// One refresh's worth of tmux state.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Snapshot {
    /// Every window row, in tmux's order (session by session, index order).
    pub windows: Vec<Window>,
    pub clients: Vec<Client>,
}

impl Snapshot {
    /// Both row kinds out of one call's stdout; anything else is skipped.
    pub fn parse(text: &str) -> Snapshot {
        let mut s = Snapshot::default();
        for line in text.lines() {
            if let Some(w) = Window::parse(line) {
                s.windows.push(w);
            } else if let Some(c) = Client::parse(line) {
                s.clients.push(c);
            }
        }
        s
    }

    /// The client with this tty.
    pub fn client(&self, tty: &str) -> Option<&Client> {
        self.clients.iter().find(|c| c.tty == tty)
    }

    /// agent-roster.py `parse_clients(text).get(client)`: the window a client is on.
    pub fn client_window(&self, tty: &str) -> Option<&str> {
        self.client(tty).map(|c| c.window_id.as_str())
    }

    /// The pulse phase: @agent_blink is a global option, so the first row's
    /// value is everyone's (agent-roster.py `render`).
    pub fn blink(&self) -> bool {
        self.windows.first().is_some_and(|w| w.blink == "1")
    }

    /// Every row of a window id (one per session it is linked into).
    pub fn rows<'a>(&'a self, id: &'a str) -> impl Iterator<Item = &'a Window> + 'a {
        self.windows.iter().filter(move |w| w.id == id)
    }

    /// The row of `id` in `session`, if the window is (still) there.
    pub fn row_in(&self, id: &str, session: &str) -> Option<&Window> {
        self.windows.iter().find(|w| w.id == id && w.session == session)
    }
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;

    /// A window row in the snapshot format (the Python tests' `line()`): a row with any FIELDS set by name; the rest default like the Python
    /// helper (name "zsh", blink "1", active "0", panes "1").
    pub fn row(session: &str, index: u32, id: &str, set: &[(&str, &str)]) -> String {
        let mut v: Vec<String> = FIELDS.iter().map(|_| String::new()).collect();
        v[0] = session.into();
        v[1] = index.to_string();
        v[2] = id.into();
        v[3] = "zsh".into();
        v[9] = "0".into();
        v[10] = "1".into();
        v[14] = "0".into();
        v[15] = "1".into();
        for (k, val) in set {
            let i = FIELDS.iter().position(|f| f == k).unwrap_or_else(|| panic!("no field {k}"));
            v[i] = val.to_string();
        }
        v.join(&US.to_string())
    }

    #[test]
    fn formats_line_up() {
        assert_eq!(FIELDS.len(), FORMATS.len());
        // The Python FMT is our first 20 fields, byte for byte (before guarding).
        let py = "#{session_name}\x1f#{window_index}\x1f#{window_id}\x1f#{window_name}\x1f#{@agent_state}\x1f#{@agent_summary}\x1f#{@agent_workflow}\x1f#{@agent_cua}\x1f#{@agent_since}\x1f#{session_last_attached}\x1f#{@agent_blink}\x1f#{@stash_label}\x1f#{@stash_session}\x1f#{@stash_ts}\x1f#{window_active}\x1f#{window_panes}\x1f#{pane_current_path}\x1f#{@agent_kind}\x1f#{@agent_detail_kind}\x1f#{@agent_detail}";
        assert!(FORMATS.join("\x1f").starts_with(py));
        assert_eq!(guarded("#{window_id}"), "#{window_id}");
        assert_eq!(guarded("#{@agent_summary}"), "#{s/\n/\u{E00A}/:#{s/\x1f/\u{E01F}/:@agent_summary}}");
        assert!(fmt().contains(&guarded("#{pane_current_path}")));
    }

    #[test]
    fn ctype_fix_only_when_needed() {
        let env = |pairs: &'static [(&'static str, &'static str)]| {
            move |k: &str| pairs.iter().find(|(n, _)| *n == k).map(|(_, v)| v.to_string())
        };
        assert!(utf8_ctype_env(env(&[("LANG", "en_US.UTF-8")])).is_empty());
        assert!(utf8_ctype_env(env(&[("LC_CTYPE", "en_US.utf8"), ("LANG", "C")])).is_empty());
        assert_eq!(utf8_ctype_env(env(&[])), vec![("LC_CTYPE", Some("en_US.UTF-8".into()))]);
        assert_eq!(utf8_ctype_env(env(&[("LANG", "C"), ("LC_COLLATE", "C")])), vec![("LC_CTYPE", Some("en_US.UTF-8".into()))]);
        assert_eq!(
            utf8_ctype_env(env(&[("LC_ALL", "C"), ("LANG", "en_US.UTF-8")])),
            vec![("LC_ALL", None), ("LC_COLLATE", Some("C".into())), ("LC_NUMERIC", Some("C".into())),
                 ("LC_CTYPE", Some("en_US.UTF-8".into()))]
        );
        // An empty LC_ALL is unset, as setlocale sees it.
        assert!(utf8_ctype_env(env(&[("LC_ALL", ""), ("LANG", "en_US.UTF-8")])).is_empty());
    }

    #[test]
    fn standins_are_restored() {
        let lf = LF_STANDIN.to_string();
        let us = US_STANDIN.to_string();
        let w = Window::parse(&row("main", 1, "@1", &[
            ("summary", &format!("a{lf}b{us}c")), ("path", &format!("/tmp/x{lf}y")),
            ("stash_label", &format!("p{lf}q")), ("detail", &format!("d{us}e")),
        ])).unwrap();
        assert_eq!((w.summary.as_str(), w.path.as_str()), ("a\nb\x1fc", "/tmp/x\ny"));
        assert_eq!((w.stash_label.as_str(), w.detail.as_str(), w.label.as_str()), ("p\nq", "d\x1fe", "a\nb\x1fc"));
        let c = Client::parse(&format!("\x1fclient\x1f/dev/x\x1f@1\x1fs{lf}t")).unwrap();
        assert_eq!(c.session, "s\nt");
    }

    #[test]
    fn parse_rows() {
        let text = [
            row("main", 1, "@1", &[("state", "idle"), ("summary", "proj/One"), ("since", "100 idle"),
                ("last_attached", "50"), ("active", "1"), ("panes", "2"), ("path", "/tmp/x"),
                ("kind", "claude"), ("detail_kind", "ask"), ("detail", "which?")]),
            row("main", 2, "@2", &[]),
            row("stash", 3, "@9", &[("summary", "stale/summary"), ("stash_label", "proj/Suspended"),
                ("stash_session", "abc-123"), ("stash_ts", "500"), ("stash_origin", "work")]),
            row("main", 4, "@10", &[("summary", "live/summary"), ("stash_label", "frozen/label")]),
            "\x1fclient\x1f/dev/ttys001\x1f@2\x1fmain".into(),
            "\x1fclient\x1f/dev/ttys002\x1f@1".into(),
        ]
        .join("\n");
        let s = Snapshot::parse(&text);
        assert_eq!(s.windows.len(), 4);
        let w = &s.windows[0];
        assert_eq!((w.index, w.since_t, w.last_attached, w.active, w.panes), (1, Some(100), 50, true, 2));
        assert_eq!((w.kind.as_str(), w.detail_kind.as_str(), w.detail.as_str()), ("claude", "ask", "which?"));
        assert_eq!(w.label, "proj/One");
        assert_eq!(s.windows[1].label, "zsh"); // name fallback
        assert_eq!(s.windows[2].label, "proj/Suspended"); // parked: the stash label wins
        assert_eq!(s.windows[2].stash_t, Some(500));
        assert_eq!(s.windows[2].stash_origin, "work");
        assert_eq!(s.windows[3].label, "live/summary"); // left the stash: label ignored
        assert_eq!(s.clients.len(), 2);
        assert_eq!(s.client_window("/dev/ttys001"), Some("@2"));
        assert_eq!(s.client("/dev/ttys001").unwrap().session, "main");
        assert_eq!(s.client("/dev/ttys002").unwrap().session, "");
        assert!(s.blink());
    }

    #[test]
    fn empty_fields_and_garbage() {
        // Every field empty: still a row, with the Python's defaults.
        let empty = vec![""; 21].join("\x1f");
        let w = Window::parse(&empty).unwrap();
        assert_eq!((w.index, w.panes, w.since_t, w.last_attached, w.active), (0, 1, None, 0, false));
        assert_eq!(w.label, "");
        // Non-numeric numbers.
        let w = Window::parse(&row("s", 0, "@1", &[("index", "x1"), ("panes", "-2"), ("since", "abc 5"),
            ("last_attached", "1.5"), ("stash_ts", "12a")])).unwrap();
        assert_eq!((w.index, w.panes, w.since_t, w.last_attached, w.stash_t), (0, 1, None, 0, None));
        // " 150 x": the Python splits on the first space only, so no stamp.
        let w = Window::parse(&row("s", 0, "@1", &[("since", " 150 x")])).unwrap();
        assert_eq!(w.since_t, None);
        // Wrong field counts, a stray US in a summary, junk lines.
        let mut bad = row("s", 1, "@1", &[]);
        bad.push('\x1f');
        let text = format!("{bad}\nhello\n\n\x1f\x1f\x1f\n\x1fclient\x1f/dev/x\n\x1fnotclient\x1f/dev/x\x1f@1\n{}",
            row("s", 1, "@1", &[("summary", "a\x1fb")]));
        let s = Snapshot::parse(&text);
        assert!(s.windows.is_empty(), "{:?}", s.windows);
        assert!(s.clients.is_empty());
        assert!(!s.blink());
        // Non-UTF-8 never gets here (decoded lossily), but odd text is fine.
        let w = Window::parse(&row("日本", 2, "@3", &[("summary", "🫠 x\tz")])).unwrap();
        assert_eq!((w.session.as_str(), w.label.as_str()), ("日本", "🫠 x\tz"));
    }

    #[test]
    fn sockets() {
        assert_eq!(Socket::parse(""), None);
        assert_eq!(Socket::parse("agentui-test"), Some(Socket::Name("agentui-test".into())));
        assert_eq!(Socket::parse("/tmp/s"), Some(Socket::Path("/tmp/s".into())));
        let p = Socket::Name("x".into()).path();
        assert!(p.ends_with(format!("tmux-{}/x", uid())), "{p:?}");
        let t = Tmux::with_socket(Some(Socket::Name("n".into())));
        let c = t.command();
        let args: Vec<_> = c.get_args().map(|a| a.to_string_lossy().into_owned()).collect();
        assert_eq!(args, ["-u", "-L", "n"]);
    }
}
