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
/// the session a parked tab came from; the menu shows it), `slot` (the
/// watcher's `@agent_slot`, the agent's sticky number key) and `pending`
/// (agent-tab-indicator.sh's `@agent_pending`: a needs-input prompt the user
/// went to answer, whose turn has not resumed yet), `key` (the menu's
/// `@agent_key`, the agent's lifetime letter) and `mru_cur` / `mru_prev`
/// (the GLOBAL `@agent_mru_cur` / `@agent_mru_prev`: the agent windows a
/// client last landed on, for the menu's ⌥W "last agent"; like
/// `@agent_blink`, every row carries the same value), and `home`
/// (spaces' `@space_home` "1": a space's landing window, never an agent row
/// nor counted in the sidebar), and the SESSION options `space_index`
/// (`@space_index`, space.sh's number for a visible space: its ⌘N) and
/// `space_hidden` (`@space_hidden` "1"): a window row's format resolves a
/// user option through its session, so every row of a session carries its
/// session's values and the sidebar's spaces list costs no extra call.
/// `space_color` (the session's `@space_color`, space.sh's `#rrggbb` for the
/// space) rides the same way, and `stash_sel` (stash.sh's `@stash_sel`: the
/// window is in prefix v's range selection), and `pane_flags` ([`PANE_FLAGS`]:
/// one token per pane, so a space's DASHBOARD pane (`@space_dash_pane`) is
/// never counted, captured, closed or focused), and `rollout`
/// (`@agent_rollout`: a Codex agent's rollout path, which the sidebar reads
/// for its live activity blurb), and `paused` (agent-pause.py's
/// `@agent_paused`: the window is in the pause key's saved set; see
/// [`crate::model::paused_state`]), and `pinned` (`@agent_pinned`: the
/// sidebar's PINNED section, [`crate::pins`]) with `session_id` (the tab
/// hook's `@agent_session_id`, a Codex agent's session: what a pin is kept
/// by across a restore), and `pin_key` (`@pin_key`: a pinned agent's ⌥W digit,
/// [`crate::pins::pin_keys`]), and a PENDING SPACE's window options
/// (space.sh `ask-claude`, docs/spaces.md "Pending spaces"): `pending_space`
/// (`@pending_space`, the space number N its claude holds), `pending_request`
/// (`@pending_request`, the words typed into the picker) and `pending_color`
/// (`@pending_color`, the colour space N will have).
pub const FIELDS: [&str; 40] = [
    "session", "index", "id", "name", "state", "summary", "workflow", "cua", "since", "last_attached",
    "blink", "stash_label", "stash_session", "stash_ts", "active", "panes", "path", "kind", "detail_kind",
    "detail", "stash_origin", "slot", "pending", "key", "mru_cur", "mru_prev", "home", "space_index",
    "space_hidden", "space_color", "stash_sel", "pane_flags", "rollout", "paused", "pinned", "session_id",
    "pin_key", "pending_space", "pending_request", "pending_color",
];

/// The pane option space.sh sets (to 1) on a space home's dashboard pane
/// (space-dash.sh, the 9-row pane above the shell). Such a pane is never an
/// agent, a shell, a count, or a click / capture / close target.
pub const DASH_OPTION: &str = "@space_dash_pane";

/// Every pane of the window as `<flag><pane_index><pane_id>:<pane_pid> `:
/// flag `A` / `p` for the active / another pane, `D` / `d` for a dashboard
/// pane ([`DASH_OPTION`]) that is / is not active; the pid of the pane's
/// process (the pins find an opencode's session under it). Only
/// `[ADdp%:0-9 ]` is printed, so it needs no guard. Verified on tmux 3.7: a
/// `#{P:}` loop in a `list-windows` format resolves pane options per pane.
/// The loop's order is tmux's internal one (pane creation, not
/// `pane_index`), so the index rides along and [`parse_pane_flags`] sorts
/// by it.
pub const PANE_FLAGS: &str =
    "#{P:#{?#{@space_dash_pane},#{?pane_active,D,d},#{?pane_active,A,p}}#{pane_index}#{pane_id}:#{pane_pid} }";

/// The tmux format of each FIELDS entry, in order. (`#{pane_current_path}`
/// is printed by [`path_format`], dashboard-aware.)
pub const FORMATS: [&str; 40] = [
    "#{session_name}", "#{window_index}", "#{window_id}", "#{window_name}", "#{@agent_state}",
    "#{@agent_summary}", "#{@agent_workflow}", "#{@agent_cua}", "#{@agent_since}", "#{session_last_attached}",
    "#{@agent_blink}", "#{@stash_label}", "#{@stash_session}", "#{@stash_ts}", "#{window_active}",
    "#{window_panes}", "#{pane_current_path}", "#{@agent_kind}", "#{@agent_detail_kind}", "#{@agent_detail}",
    "#{@stash_origin}", "#{@agent_slot}", "#{@agent_pending}", "#{@agent_key}", "#{@agent_mru_cur}",
    "#{@agent_mru_prev}", "#{@space_home}", "#{@space_index}", "#{@space_hidden}", "#{@space_color}", "#{@stash_sel}",
    PANE_FLAGS, "#{@agent_rollout}", "#{@agent_paused}", "#{@agent_pinned}", "#{@agent_session_id}",
    "#{@pin_key}", "#{@pending_space}", "#{@pending_request}", "#{@pending_color}",
];

/// Ends each path in the dashboard case of [`path_format`] (a private-use
/// code point, like the stand-ins).
pub const PATH_SEP: char = '\u{E01E}';

/// The `path` field: the active pane's cwd, unless the active pane is a
/// dashboard; then every other pane's `<pane_index> <cwd>`, each ended by
/// [`PATH_SEP`], of which [`dash_path`] keeps the lowest index's (the
/// shell under the dashboard, where spaces.conf's hook puts focus).
pub fn path_format() -> String {
    let g = guarded("#{pane_current_path}");
    format!("#{{?#{{{DASH_OPTION}}},#{{P:#{{?#{{{DASH_OPTION}}},,#{{pane_index}} {g}{PATH_SEP}}}}},{g}}}")
}

/// The printed `path` field → the path: as is, or in the dashboard case
/// ([`PATH_SEP`] present) the lowest-indexed other pane's.
pub fn dash_path(field: &str) -> String {
    if !field.contains(PATH_SEP) {
        return field.to_string();
    }
    field
        .split(PATH_SEP)
        .filter_map(|e| e.split_once(' '))
        .filter_map(|(i, p)| i.parse::<u32>().ok().map(|i| (i, p)))
        .min_by_key(|(i, _)| *i)
        .map(|(_, p)| p.to_string())
        .unwrap_or_default()
}

/// One pane of a window row ([`PANE_FLAGS`]).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pane {
    /// `%N`.
    pub id: String,
    /// `pane_index`.
    pub index: u32,
    pub active: bool,
    /// A dashboard pane ([`DASH_OPTION`]).
    pub dash: bool,
    /// `pane_pid` (None from an older token without it).
    pub pid: Option<i32>,
}

/// [`PANE_FLAGS`]' output → the panes in `pane_index` order; tokens that
/// are not `<flag><digits>%<digits>[:<digits>]` are skipped.
pub fn parse_pane_flags(s: &str) -> Vec<Pane> {
    let digits = |s: &str| !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit());
    let mut v: Vec<Pane> = s
        .split_whitespace()
        .filter_map(|t| {
            let (flag, rest) = (t.chars().next()?, t.get(1..)?);
            let (index, num) = rest.split_once('%')?;
            let (num, pid) = match num.split_once(':') {
                Some((n, p)) if digits(p) => (n, p.parse::<i32>().ok()),
                Some(_) => return None,
                None => (num, None),
            };
            if !digits(index) || !digits(num) {
                return None;
            }
            let (active, dash) = match flag {
                'A' => (true, false),
                'p' => (false, false),
                'D' => (true, true),
                'd' => (false, true),
                _ => return None,
            };
            Some(Pane { id: format!("%{num}"), index: index.parse().ok()?, active, dash, pid })
        })
        .collect();
    v.sort_by_key(|p| p.index);
    v
}

/// Stand-ins tmux writes for a newline / US inside a field value (Unicode
/// private-use code points), so free text can never split a row or add a
/// field. [`restore`] maps them back. A value that really contains one of
/// these code points is read as a newline / US; nothing writes them.
pub const LF_STANDIN: char = '\u{E00A}';
pub const US_STANDIN: char = '\u{E01F}';

/// Fields that are always tmux-generated digits / ids, never free text.
/// (`@agent_slot` is a user option, so anyone can set it to anything: it is
/// guarded like free text and parsed strictly, [`parse_slot`].)
const PLAIN_FIELDS: [&str; 6] =
    ["#{window_index}", "#{window_id}", "#{session_last_attached}", "#{window_active}", "#{window_panes}", PANE_FLAGS];

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
    FORMATS
        .iter()
        .map(|f| if *f == "#{pane_current_path}" { path_format() } else { guarded(f) })
        .collect::<Vec<_>>()
        .join(&US.to_string())
}

/// Client rows ride in the same call. agent-roster.py `CLIENT_FMT` plus two:
/// `"" US client US tty US window_id US session US @spaces US @strip_scope
/// US @space_hidden US client_pid`. `@spaces` and `@strip_scope` are GLOBAL
/// options and `@space_hidden` the client's session's (the sidebar's scope,
/// [`crate::view::Scope`]): a client-context format resolves them through
/// the client's session (to the global value), so they cost no extra call.
/// `client_pid` lets the sidebar watch its client process exit.
/// Then [`STATUS_FORMATS`]: the client's key table (`prefix` / `jump`) and
/// the GLOBAL options the sidebar's status block shows in place of the
/// hidden status line (usage, the stale-save mark, the open-sidebar count).
/// Starts with an empty field and has 9 + [`STATUS_FORMATS`] fields where a
/// window row has [`FIELDS`]' 40, so the two parsers can never take each other's rows.
pub const CLIENT_TAG: &str = "client";

/// The client row's tail, in [`Status`] field order: what the sidebar draws
/// instead of status-right, read in the same one call (no script runs).
pub const STATUS_FORMATS: [&str; 33] = [
    "#{client_key_table}", "#{@sidebar_open}", "#{@sidebar_pids}", "#{@resurrect_stale}",
    "#{@codex_session_text}", "#{@codex_session_color}", "#{@codex_weekly_text}", "#{@codex_weekly_color}",
    "#{@codex_scoped_icon}", "#{@codex_scoped_text}", "#{@codex_scoped_color}", "#{@codex_account_label}",
    "#{@codex_provider_label}", "#{status}",
    // The usage meters' numbers ([`Meter`]), per window: session, weekly, scoped.
    "#{@codex_session_used}", "#{@codex_session_expected}", "#{@codex_session_delta}", "#{@codex_session_resets}",
    "#{@codex_session_members}",
    "#{@codex_weekly_used}", "#{@codex_weekly_expected}", "#{@codex_weekly_delta}", "#{@codex_weekly_resets}",
    "#{@codex_weekly_members}",
    "#{@codex_scoped_used}", "#{@codex_scoped_expected}", "#{@codex_scoped_delta}", "#{@codex_scoped_resets}",
    "#{@codex_scoped_members}",
    // notify.sh's toasts (up to 4 RS-separated records, each `seq TAB
    // epoch_ms TAB kind TAB ttl TAB client TAB text [TAB title]`) and the
    // open sidebars' clients, beside `@sidebar_pids`.
    "#{@toast}", "#{@sidebar_clients}",
    // The columns the sidebars take from the WezTerm row (popups centre on it).
    "#{@sidebar_width}",
    // When this server started (epoch seconds): window ids restart at @0 in
    // a new server, so the pause banner trusts a saved id only from a pause
    // made since ([`crate::sidebar::pause`]).
    "#{start_time}",
];

/// One usage window's numeric options (`@codex_<w>_used`, `_expected`,
/// `_delta`, `_resets`, `_members`), raw; "" when unset (the sidebar then
/// parses `@codex_<w>_text`).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Meter {
    pub used: String,
    pub expected: String,
    pub delta: String,
    pub resets: String,
    pub members: String,
}

/// A full client row's field count.
pub const CLIENT_FIELDS: usize = 9 + STATUS_FORMATS.len();

pub fn client_fmt() -> String {
    let mut v: Vec<String> = ["", CLIENT_TAG, "#{client_tty}", "#{window_id}"].map(String::from).to_vec();
    v.extend(["#{session_name}", "#{@spaces}", "#{@strip_scope}", "#{@space_hidden}"].map(guarded));
    v.push("#{client_pid}".into());
    v.extend(STATUS_FORMATS.map(guarded));
    v.join(&US.to_string())
}

/// The status-line values a client row carries ([`STATUS_FORMATS`]), raw
/// ("" = unset, or an older, shorter row).
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Status {
    /// `#{client_key_table}`: "root", "prefix", "jump" (UB's ⌥W table), ...
    pub key_table: String,
    /// `@sidebar_open`: how many sidebars are open (the sidebar keeps it).
    pub sidebar_open: String,
    /// `@sidebar_pids`: the open sidebars' pids, space-separated.
    pub sidebar_pids: String,
    /// `@resurrect_stale`: "2h" (the last save's age), "refused", or "".
    pub resurrect_stale: String,
    pub session_text: String,
    pub session_color: String,
    pub weekly_text: String,
    pub weekly_color: String,
    pub scoped_icon: String,
    pub scoped_text: String,
    pub scoped_color: String,
    pub account_label: String,
    pub provider_label: String,
    /// The `status` option as the client's session sees it ("on", "off", "2"…).
    pub status_line: String,
    /// The meters' numbers: session, weekly, scoped.
    pub meters: [Meter; 3],
    /// `@toast`: notify.sh's recent toasts, raw (`status::Toast::parse_all`).
    pub toast: String,
    /// `@sidebar_clients`: the open sidebars' clients, word for word beside
    /// `@sidebar_pids` (`-`: none resolved yet).
    pub sidebar_clients: String,
    /// `@sidebar_width`: the leading sidebar's columns plus WezTerm's divider
    /// (tmux.conf.tmpl's `@popup_x` reads it); "" when unset.
    pub sidebar_width: String,
    /// `#{start_time}`: when the server started (epoch seconds), raw.
    pub server_start: String,
}

impl Status {
    fn from_fields(f: &dyn Fn(usize) -> String) -> Status {
        Status {
            key_table: f(0),
            sidebar_open: f(1),
            sidebar_pids: f(2),
            resurrect_stale: f(3),
            session_text: f(4),
            session_color: f(5),
            weekly_text: f(6),
            weekly_color: f(7),
            scoped_icon: f(8),
            scoped_text: f(9),
            scoped_color: f(10),
            account_label: f(11),
            provider_label: f(12),
            status_line: f(13),
            meters: [0, 1, 2].map(|k| {
                let at = 14 + 5 * k;
                Meter { used: f(at), expected: f(at + 1), delta: f(at + 2), resets: f(at + 3), members: f(at + 4) }
            }),
            toast: f(29),
            sidebar_clients: f(30),
            sidebar_width: f(31),
            server_start: f(32),
        }
    }
}

/// The holding session for parked tabs (agent-roster.py `HOLD`).
pub const HOLD: &str = "stash";

/// The hidden session holding every PENDING SPACE's claude (space.sh
/// `PEND_SESS`, docs/spaces.md "Pending spaces"): the one hidden session the
/// sidebar lists, and only its windows with `@pending_space` set
/// ([`Window::is_pending`]).
pub const PENDING: &str = "pending";

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
    /// @agent_state: idle | running | needs-input | failed | done | paused |
    /// "" (no agent), with the pause folded in: `done` / `idle` on a window
    /// that has `@agent_paused` set reads `paused` ([`crate::model::paused_state`]),
    /// so every reader sees one state.
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
    /// How many panes, dashboard panes ([`DASH_OPTION`]) NOT counted: from
    /// [`PANE_FLAGS`] when the row has them, else window_panes (1 when
    /// unparseable).
    pub panes: u32,
    /// pane_current_path of the window's active pane; when that is a
    /// dashboard, of its lowest-indexed other pane ([`path_format`]).
    pub path: String,
    /// Every pane ([`PANE_FLAGS`]) in `pane_index` order, dashboards
    /// included and marked; empty for a row without the field's tokens
    /// (tests, junk).
    pub pane_list: Vec<Pane>,
    /// @agent_kind: claude | codex | opencode | "".
    pub kind: String,
    /// @agent_detail_kind: perm | ask | fail | done | run | "".
    pub detail_kind: String,
    pub detail: String,
    /// agent-roster.py `label`: @stash_label (parked rows only), else the
    /// summary, else the window name.
    pub label: String,
    /// @agent_slot as stamped by the watcher, when valid ([`parse_slot`]).
    /// Only a stamp: the number a UI shows is [`crate::view::assign_slots`]'s,
    /// which drops stale stamps and fills agents the watcher has not reached.
    pub slot: Option<u32>,
    /// Raw @agent_pending ("" = unset): with state `idle` and a fresh epoch,
    /// a prompt the user just answered whose turn has not resumed
    /// ([`Window::answered`]).
    pub pending: String,
    /// Raw @agent_key: the menu's lifetime letter for this window ("" =
    /// none yet). Only a stamp: the letter shown is
    /// [`crate::menu::letters::assign`]'s.
    pub key: String,
    /// Raw @agent_mru_cur / @agent_mru_prev (global options: the same on
    /// every row): window ids like `@12`, or "".
    pub mru_cur: String,
    pub mru_prev: String,
    /// @space_home is "1": a space's home (landing) window, never an agent.
    pub home: bool,
    /// The window's SESSION's @space_index (1-based, digits only; None when
    /// unset or junk): the space's number, ⌘N.
    pub space_index: Option<u32>,
    /// The window's SESSION has @space_hidden "1" (not a space).
    pub space_hidden: bool,
    /// The window's SESSION's @space_color (space.sh's `#rrggbb`), raw.
    pub space_color: String,
    /// @stash_sel is set: in stash.sh's range selection (prefix v).
    pub stash_sel: bool,
    /// @agent_rollout: a Codex agent's rollout (its transcript) path, as
    /// agent-tab-indicator.sh stashes it; "" for any other.
    pub rollout: String,
    /// Raw @agent_paused (agent-pause.py: `<epoch>` while the window is in
    /// the pause key's saved set; "" = unset). Already folded into `state`.
    pub paused: String,
    /// `@agent_pinned`: None when unset; else when it was pinned (epoch
    /// seconds; 0 for a value that is not all digits, still pinned).
    pub pinned: Option<i64>,
    /// `@agent_session_id` (the tab hook's, for a Codex agent), trimmed.
    pub session_id: String,
    /// Raw `@pin_key` ("" = none): a pinned agent's ⌥W digit as stored. Only a
    /// stamp: the number shown is [`crate::pins::pin_keys`]'.
    pub pin_key: String,
    /// `@pending_space`: the space number a pending space's claude holds
    /// (digits, no leading zero; None when unset or junk). Only counts in
    /// the [`PENDING`] session ([`Window::is_pending`]).
    pub pending_space: Option<u32>,
    /// `@pending_request`: what was typed into the new-space picker, raw.
    pub pending_request: String,
    /// `@pending_color`: the `#rrggbb` space N will have, trimmed.
    pub pending_color: String,
}

fn digits(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_digit())
}

/// An @agent_slot value → the slot, when it is VALID by the watcher's rule
/// (agent-tab-watcher.sh `^[1-9][0-9]{0,8}$`): 1 to 9 decimal digits, no
/// leading zero. Empty, 0, `007`, signs, spaces and junk are all None (the
/// watcher reassigns such a window, so we must not keep its number either).
pub fn parse_slot(s: &str) -> Option<u32> {
    if !digits(s) || s.len() > 9 || s.starts_with('0') {
        return None;
    }
    s.parse::<u32>().ok()
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
        let pane_list = parse_pane_flags(p[31]);
        let panes = if pane_list.is_empty() {
            num(p[15]).and_then(|n| u32::try_from(n).ok()).unwrap_or(1)
        } else {
            pane_list.iter().filter(|q| !q.dash).count() as u32
        };
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
            state: crate::model::paused_state(p[4], p[33]).into(),
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
            panes,
            path: dash_path(p[16]),
            pane_list,
            kind: p[17].into(),
            detail_kind: p[18].into(),
            detail: p[19].into(),
            stash_origin: p[20].into(),
            slot: parse_slot(p[21]),
            pending: p[22].into(),
            key: p[23].into(),
            mru_cur: p[24].into(),
            mru_prev: p[25].into(),
            home: p[26] == "1",
            space_index: parse_slot(p[27]),
            space_hidden: p[28] == "1",
            space_color: p[29].trim().into(),
            stash_sel: !p[30].is_empty(),
            rollout: p[32].trim().into(),
            paused: p[33].into(),
            pinned: Some(p[34].trim()).filter(|s| !s.is_empty()).map(|s| num(s).unwrap_or(0)),
            session_id: p[35].trim().into(),
            pin_key: p[36].trim().into(),
            pending_space: parse_slot(p[37].trim()),
            pending_request: p[38].into(),
            pending_color: p[39].trim().into(),
            label: label.into(),
            session,
        })
    }

    pub fn is_parked(&self) -> bool {
        self.session == HOLD
    }

    /// A PENDING SPACE's claude: in the hidden [`PENDING`] session with
    /// `@pending_space` set. Going to it opens its popup (`space.sh
    /// pending-open`), never selects it; adoption clears the option and
    /// moves the window into its space, where it is an ordinary agent.
    pub fn is_pending(&self) -> bool {
        self.session == PENDING && self.pending_space.is_some()
    }

    /// The window has a dashboard pane ([`DASH_OPTION`]).
    pub fn has_dash(&self) -> bool {
        self.pane_list.iter().any(|p| p.dash)
    }

    /// The pane focus must move to when this window is selected: its
    /// lowest-indexed non-dashboard pane (the shell under the dashboard,
    /// as spaces.conf's `{down-of}`), but only while a DASHBOARD is the active pane
    /// (None otherwise: the active pane is already right, or there is no
    /// other pane).
    pub fn focus_pane(&self) -> Option<&str> {
        if !self.pane_list.iter().any(|p| p.dash && p.active) {
            return None;
        }
        self.pane_list.iter().find(|p| !p.dash).map(|p| p.id.as_str())
    }

    /// What a capture of "the window" targets: the window id (its active
    /// pane) unless it has a dashboard; then its active non-dashboard pane,
    /// else its lowest-indexed one (the window id when it has none).
    pub fn capture_target(&self) -> String {
        if !self.has_dash() {
            return self.id.clone();
        }
        let mut real = self.pane_list.iter().filter(|p| !p.dash);
        let first = real.clone().next();
        real.find(|p| p.active).or(first).map_or_else(|| self.id.clone(), |p| p.id.clone())
    }

    /// A prompt the user just answered, at `now` (epoch seconds): "idle AND
    /// @agent_pending is an epoch no older than 600 s" (the watcher's rule,
    /// [`ANSWERED_TTL`]). Its turn has not resumed yet, so the agent still
    /// counts as Active and keeps its slot. Age is whole seconds, as the
    /// watcher's `$(( now - pend ))`; a stamp from the future counts as
    /// fresh. Empty or junk (anything but digits) is never answered, and an
    /// old stamp (an answer-by-No that never resumed, a seen failure) has
    /// lapsed, so that agent lets its number go.
    pub fn answered(&self, now: f64) -> bool {
        if self.state != "idle" || !digits(&self.pending) {
            return false;
        }
        match self.pending.parse::<i64>() {
            Ok(t) => (now.floor() as i64).saturating_sub(t) <= ANSWERED_TTL,
            Err(_) => false,
        }
    }
}

/// How long an answered prompt (`@agent_pending`) keeps an idle agent
/// Active, in seconds (agent-tab-watcher.sh uses the same 600).
pub const ANSWERED_TTL: i64 = 600;

/// One `list-clients` row.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Client {
    pub tty: String,
    pub window_id: String,
    /// "" when a 4-field (old Python-format) row was parsed.
    pub session: String,
    /// The global `@spaces` and `@strip_scope` as this row printed them ("":
    /// unset, or a row of an older, shorter format).
    pub spaces: String,
    pub strip_scope: String,
    /// The client's session has `@space_hidden` "1" (spaces.conf marks the
    /// hidden sessions so).
    pub space_hidden: bool,
    /// The client process (`#{client_pid}`): the sidebar leaves when it exits.
    pub pid: Option<i32>,
    /// The key table and the global status options ([`STATUS_FORMATS`]).
    pub status: Status,
}

impl Client {
    /// agent-roster.py `parse_client_rows`, one row:
    /// `"" US client US tty US win [US session [US spaces US scope [US space_hidden [US pid]]]]`.
    pub fn parse(line: &str) -> Option<Client> {
        let p: Vec<&str> = line.split(US).collect();
        if matches!(p.len(), 4 | 5 | 7 | 8 | 9 | CLIENT_FIELDS) && p[0].is_empty() && p[1] == CLIENT_TAG {
            let f = |i: usize| restore(p.get(i).unwrap_or(&""));
            let pid = p.get(8).filter(|s| digits(s)).and_then(|s| s.parse::<i32>().ok()).filter(|&n| n > 0);
            let status = Status::from_fields(&|i| f(9 + i));
            Some(Client { tty: p[2].into(), window_id: p[3].into(), session: f(4), spaces: f(5), strip_scope: f(6),
                space_hidden: f(7) == "1", pid, status })
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

    /// @agent_mru_cur and @agent_mru_prev (global options, so the first
    /// row's values are everyone's); "" = unset.
    pub fn mru(&self) -> (&str, &str) {
        self.windows.first().map_or(("", ""), |w| (w.mru_cur.trim(), w.mru_prev.trim()))
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
    fn client_rows_carry_the_scope_options() {
        // (A guarded format holds a US itself, so compare it whole.)
        let want = ["", CLIENT_TAG, "#{client_tty}", "#{window_id}", &guarded("#{session_name}"), &guarded("#{@spaces}"),
            &guarded("#{@strip_scope}"), &guarded("#{@space_hidden}"), "#{client_pid}"].join("\x1f");
        let tail: Vec<String> = STATUS_FORMATS.iter().map(|f| guarded(f)).collect();
        assert_eq!(client_fmt(), format!("{want}\x1f{}", tail.join("\x1f")));
        // A full row: the key table and the status options, in order.
        let mut full: Vec<String> = ["", "client", "/dev/g", "@9", "main", "on", "", "", "77"].map(String::from).to_vec();
        full.extend((0..STATUS_FORMATS.len()).map(|i| format!("v{i}")));
        let c = Client::parse(&full.join("\x1f")).unwrap();
        assert_eq!((c.pid, c.status.key_table.as_str(), c.status.sidebar_open.as_str(), c.status.provider_label.as_str()),
            (Some(77), "v0", "v1", "v12"));
        assert_eq!(c.status.status_line, "v13");
        // The meters' numbers: five per window, session then weekly then scoped.
        assert_eq!(c.status.meters[0], Meter { used: "v14".into(), expected: "v15".into(), delta: "v16".into(),
            resets: "v17".into(), members: "v18".into() });
        assert_eq!((c.status.meters[1].used.as_str(), c.status.meters[2].members.as_str()), ("v19", "v28"));
        assert_eq!((c.status.toast.as_str(), c.status.sidebar_clients.as_str()), ("v29", "v30"));
        assert_eq!((c.status.sidebar_width.as_str(), c.status.server_start.as_str()), ("v31", "v32"));
        // One field short or long: not a client row.
        assert!(Client::parse(&full[..full.len() - 1].join("\x1f")).is_none());
        let s = Snapshot::parse(&[
            "\x1fclient\x1f/dev/a\x1f@3\x1fmain\x1fon\x1fall\x1f\x1f4242",
            "\x1fclient\x1f/dev/b\x1f@4\x1fwork",
            "\x1fclient\x1f/dev/c\x1f@5",
            "\x1fclient\x1f/dev/d\x1f@6\x1fmain\x1fon", // 6 fields: no format ever printed that
            "\x1fclient\x1f/dev/e\x1f@7\x1fscratch\x1fon\x1f\x1f1",
            "\x1fclient\x1f/dev/f\x1f@8\x1fmain\x1fon\x1fall", // agent-roster.py's 7-field row
        ]
        .join("\n"));
        let got: Vec<_> = s.clients.iter()
            .map(|c| (c.tty.as_str(), c.session.as_str(), c.spaces.as_str(), c.strip_scope.as_str(), c.space_hidden))
            .collect();
        assert_eq!(got, [("/dev/a", "main", "on", "all", false), ("/dev/b", "work", "", "", false),
            ("/dev/c", "", "", "", false), ("/dev/e", "scratch", "on", "", true), ("/dev/f", "main", "on", "all", false)]);
        let pids: Vec<_> = s.clients.iter().map(|c| c.pid).collect();
        assert_eq!(pids, [Some(4242), None, None, None, None]);
    }

    #[test]
    fn empty_fields_and_garbage() {
        // Every field empty: still a row, with the Python's defaults.
        let empty = vec![""; FIELDS.len()].join("\x1f");
        let w = Window::parse(&empty).unwrap();
        assert_eq!((w.index, w.panes, w.since_t, w.last_attached, w.active, w.slot), (0, 1, None, 0, false, None));
        // The rows of an older format are not rows.
        assert!(Window::parse(&vec![""; 21].join("\x1f")).is_none());
        assert!(Window::parse(&vec![""; 22].join("\x1f")).is_none());
        assert!(Window::parse(&vec![""; 23].join("\x1f")).is_none());
        assert!(Window::parse(&vec![""; 25].join("\x1f")).is_none());
        assert!(Window::parse(&vec![""; 26].join("\x1f")).is_none());
        assert!(Window::parse(&vec![""; 27].join("\x1f")).is_none());
        assert!(Window::parse(&vec![""; 29].join("\x1f")).is_none());
        assert!(Window::parse(&vec![""; 30].join("\x1f")).is_none());
        assert!(Window::parse(&vec![""; 31].join("\x1f")).is_none());
        assert_eq!(w.label, "");
        // Answered: idle with a @agent_pending epoch at most 600 s old.
        let now = 1_700_000_000.0;
        let ans = |st: &str, p: &str, now: f64| {
            Window::parse(&row("s", 1, "@1", &[("state", st), ("pending", p)])).unwrap().answered(now)
        };
        assert!(ans("idle", "1700000000", now)); // 0 s
        assert!(ans("idle", "1699999400", now)); // 600 s: holds
        assert!(!ans("idle", "1699999399", now)); // 601 s: lapsed
        assert!(ans("idle", "1699999400", now + 0.9)); // whole seconds, as the watcher's $(( ))
        assert!(ans("idle", "1700000100", now)); // from the future: fresh
        // Junk or empty is never answered; other states never are.
        for junk in ["", "x", "1700000000x", " 1700000000", "-5", "1.5", "99999999999999999999"] {
            assert!(!ans("idle", junk, now), "{junk:?}");
        }
        assert!(!ans("running", "1700000000", now) && !ans("", "1700000000", now) && !ans("needs-input", "1700000000", now));
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

    /// A row from an older format (37 fields, before the pending space's
    /// options; 34, before `pinned`, `session_id` and `pin_key`) or one with
    /// a field too many is no window: never misread by position.
    #[test]
    fn rows_of_the_wrong_length_are_rejected() {
        assert_eq!(FIELDS.len(), 40);
        for n in [34, 36, 37, 38, 39, 41] {
            assert!(Window::parse(&vec!["x"; n].join("\x1f")).is_none(), "{n}");
        }
        assert!(Window::parse(&vec!["x"; 40].join("\x1f")).is_some());
        assert_ne!(FIELDS.len(), CLIENT_FIELDS);
    }

    /// A pending space's options: only a window of the `pending` session
    /// with a valid `@pending_space` is one; the request is kept raw (a
    /// newline in it survives the row), the colour trimmed.
    #[test]
    fn pending_space_options() {
        let set = [("pending_space", "4"), ("pending_request", "make a\nrust cli"), ("pending_color", " #f5c2e7 ")];
        let w = Window::parse(&row(PENDING, 1, "@7", &set)).unwrap();
        assert!(w.is_pending());
        assert_eq!((w.pending_space, w.pending_request.as_str(), w.pending_color.as_str()), (Some(4), "make a\nrust cli", "#f5c2e7"));
        // Elsewhere (adopted: the options are cleared anyway), or junk: none.
        assert!(!Window::parse(&row("main", 1, "@7", &set)).unwrap().is_pending());
        for junk in ["", "0", "04", "x"] {
            assert!(!Window::parse(&row(PENDING, 1, "@7", &[("pending_space", junk)])).unwrap().is_pending(), "{junk:?}");
        }
        assert!(fmt().contains("@pending_request") && fmt().contains("@pending_color"));
    }

    /// `@agent_pinned`: unset is None; digits are the pin time; anything
    /// else non-empty still pins (time 0). `@agent_session_id` rides along.
    #[test]
    fn the_pin_and_the_session_id() {
        let w = |pin: &str| Window::parse(&row("main", 1, "@1", &[("pinned", pin), ("session_id", " abc-1 ")])).unwrap();
        assert_eq!(w("").pinned, None);
        assert_eq!(w("1792418628").pinned, Some(1_792_418_628));
        assert_eq!(w(" 12 ").pinned, Some(12));
        assert_eq!(w("yes").pinned, Some(0));
        assert_eq!(w("").session_id, "abc-1");
        assert!(fmt().contains(&format!("{}\x1f{}\x1f{}\x1f", guarded("#{@agent_pinned}"), guarded("#{@agent_session_id}"),
            guarded("#{@pin_key}"))));
        assert_eq!(Window::parse(&row("main", 1, "@1", &[("pin_key", " 3 ")])).unwrap().pin_key, "3");
    }

    /// `@agent_paused` folds into the state: done / idle read paused, a
    /// window still running or asking keeps its live state, `paused` as
    /// written stays paused, and a stray flag on a plain shell is nothing.
    #[test]
    fn the_pause_flag_folds_into_the_state() {
        let st = |state: &str, flag: &str| Window::parse(&row("main", 1, "@1", &[("state", state), ("paused", flag)])).unwrap();
        for (state, flag, want) in [
            ("paused", "", "paused"),
            ("paused", "1792418628", "paused"),
            ("done", "1792418628", "paused"),
            ("idle", "1792418628", "paused"),
            ("running", "1792418628", "running"),
            ("needs-input", "1792418628", "needs-input"),
            ("failed", "1792418628", "failed"),
            ("", "1792418628", ""),
            ("done", "", "done"),
            ("idle", "", "idle"),
        ] {
            let w = st(state, flag);
            assert_eq!(w.state, want, "{state:?} + {flag:?}");
            assert_eq!(w.paused, flag);
        }
        assert!(fmt().contains(&guarded("#{@agent_paused}")));
    }

    #[test]
    fn slots() {
        assert!(fmt().contains(&guarded("#{@agent_slot}")) && fmt().contains(&guarded("#{@agent_pending}")));
        assert!(fmt().contains(&guarded("#{@agent_key}")) && fmt().contains(&guarded("#{@agent_mru_prev}")));
        assert!(fmt().contains(&guarded("#{@space_home}")));
        assert!(fmt().contains(&format!("{}\x1f{}\x1f{}\x1f{}\x1f{PANE_FLAGS}\x1f{}\x1f{}\x1f{}\x1f{}\x1f{}\x1f", guarded("#{@space_index}"),
            guarded("#{@space_hidden}"), guarded("#{@space_color}"), guarded("#{@stash_sel}"), guarded("#{@agent_rollout}"),
            guarded("#{@agent_paused}"), guarded("#{@agent_pinned}"), guarded("#{@agent_session_id}"), guarded("#{@pin_key}"))));
        // Then a pending space's options, last.
        assert!(fmt().ends_with(&format!("{}\x1f{}\x1f{}", guarded("#{@pending_space}"), guarded("#{@pending_request}"),
            guarded("#{@pending_color}"))));
        let s = Snapshot::parse(&row("main", 1, "@1", &[("rollout", "/x/rollout-1.jsonl")]));
        assert_eq!(s.windows[0].rollout, "/x/rollout-1.jsonl");
        let s = Snapshot::parse(&row("main", 1, "@1", &[("space_color", " #f9e2af "), ("stash_sel", "1")]));
        assert_eq!((s.windows[0].space_color.as_str(), s.windows[0].stash_sel), ("#f9e2af", true));
        let s = Snapshot::parse(&[row("main", 1, "@1", &[("home", "1")]), row("main", 2, "@2", &[("home", "0")])].join("\n"));
        assert_eq!((s.windows[0].home, s.windows[1].home), (true, false));
        // The session's space number and hidden mark, on each of its rows.
        let s = Snapshot::parse(&[row("a", 1, "@1", &[("space_index", "3")]), row("b", 1, "@2", &[("space_index", "x")]),
            row("c", 1, "@3", &[("space_hidden", "1")])].join("\n"));
        let got: Vec<_> = s.windows.iter().map(|w| (w.space_index, w.space_hidden)).collect();
        assert_eq!(got, [(Some(3), false), (None, false), (None, true)]);
        // The menu's letter and the global MRU pair ride in the same rows.
        let s = Snapshot::parse(&[row("main", 1, "@1", &[("key", "f"), ("mru_cur", "@4"), ("mru_prev", " @2 ")]),
            row("main", 2, "@2", &[("mru_cur", "@4"), ("mru_prev", "@2")])].join("\n"));
        assert_eq!((s.windows[0].key.as_str(), s.windows[1].key.as_str()), ("f", ""));
        assert_eq!(s.mru(), ("@4", "@2"));
        assert_eq!(Snapshot::default().mru(), ("", ""));
        for (raw, want) in [("1", Some(1)), ("7", Some(7)), ("12", Some(12)), ("999999999", Some(999_999_999)),
            ("007", None), ("1000000000", None), ("", None), ("0", None), ("00", None), ("-3", None), ("+3", None),
            (" 3", None), ("3 ", None), ("3a", None), ("x", None), ("1.5", None), ("4294967296", None), ("٣", None)] {
            assert_eq!(parse_slot(raw), want, "{raw:?}");
            let w = Window::parse(&row("main", 1, "@1", &[("slot", raw)])).unwrap();
            assert_eq!(w.slot, want, "{raw:?}");
        }
        // A newline in the option is a stand-in in the row: junk, never a split row.
        let lf = LF_STANDIN.to_string();
        let s = Snapshot::parse(&row("main", 1, "@1", &[("slot", &format!("3{lf}4"))]));
        assert_eq!((s.windows.len(), s.windows[0].slot), (1, None));
    }

    /// A space home's dashboard pane (`@space_dash_pane`): never counted;
    /// the path is the shell's even while the dashboard is active; focus
    /// and captures go to the shell pane.
    #[test]
    fn dashboard_panes_are_filtered() {
        assert_eq!(guarded(PANE_FLAGS), PANE_FLAGS);
        assert!(fmt().contains(&path_format()) && path_format().contains(&guarded("#{pane_current_path}")));
        // Sorted by pane_index (tmux loops in creation order); junk skipped.
        assert_eq!(parse_pane_flags("p2%9 D0%3 p1%4 x3%5 A4% p5%x A%6 p%7"), [
            Pane { id: "%3".into(), index: 0, active: true, dash: true, pid: None },
            Pane { id: "%4".into(), index: 1, active: false, dash: false, pid: None },
            Pane { id: "%9".into(), index: 2, active: false, dash: false, pid: None },
        ]);
        // With the pane's pid (today's format); a junk pid drops the token.
        assert_eq!(parse_pane_flags("A0%3:4242 p1%4:x p2%5:"), [
            Pane { id: "%3".into(), index: 0, active: true, dash: false, pid: Some(4242) },
        ]);
        let sep = PATH_SEP.to_string();
        assert_eq!(dash_path("/plain path"), "/plain path");
        assert_eq!(dash_path(&format!("2 /b c{sep}1 /a b{sep}")), "/a b");
        assert_eq!(dash_path(&format!("x /a{sep}")), "");
        // The home: dashboard on top (active), the shell below.
        let w = Window::parse(&row("main", 0, "@1", &[("home", "1"), ("panes", "2"), ("pane_flags", "D0%3 p1%4 "),
            ("path", &format!("1 /shell/cwd{sep}"))])).unwrap();
        assert_eq!((w.panes, w.path.as_str(), w.has_dash()), (1, "/shell/cwd", true));
        assert_eq!((w.focus_pane(), w.capture_target().as_str()), (Some("%4"), "%4"));
        // The shell active: nothing to move; captures still skip the dashboard.
        let w = Window::parse(&row("main", 0, "@1", &[("panes", "3"), ("pane_flags", "d0%3 p1%4 A2%5 "), ("path", "/b")]))
            .unwrap();
        assert_eq!((w.panes, w.path.as_str(), w.focus_pane(), w.capture_target().as_str()), (2, "/b", None, "%5"));
        // Several shells under an active dashboard, a newer pane at the
        // lower index (split above): the lowest-indexed one's path and pane.
        let w = Window::parse(&row("main", 0, "@1", &[("pane_flags", "D0%3 p2%4 p1%5 "),
            ("path", &format!("2 /old{sep}1 /new{sep}"))])).unwrap();
        assert_eq!((w.panes, w.path.as_str(), w.focus_pane()), (2, "/new", Some("%5")));
        // Only a dashboard: no pane counted, no other pane to go to.
        let w = Window::parse(&row("main", 0, "@1", &[("pane_flags", "D0%3 "), ("path", "")])).unwrap();
        assert_eq!((w.panes, w.focus_pane(), w.capture_target().as_str()), (0, None, "@1"));
        // No dashboard: window_panes stands, the window is its own target.
        let w = Window::parse(&row("main", 0, "@1", &[("panes", "2"), ("pane_flags", "A0%1 p1%2 ")])).unwrap();
        assert_eq!((w.panes, w.has_dash(), w.focus_pane(), w.capture_target().as_str()), (2, false, None, "@1"));
        // A row without the tokens: window_panes, as before.
        let w = Window::parse(&row("main", 0, "@1", &[("panes", "4")])).unwrap();
        assert_eq!((w.panes, w.pane_list.len()), (4, 0));
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
