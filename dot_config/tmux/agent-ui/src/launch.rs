//! `agent-ui launch`: ⌘E / ⌘⇧E, the agent launcher popup. One row per
//! INSTALLED coding agent (claude first and preselected): its logo, name and
//! command, its letter as a solid key-cap at the row's right end; ⏎ or the
//! letter opens it in a new tab.
//!
//! ```text
//! agent-ui launch --client <tty> [--pane <%id>] [--pick-space]
//! agent-ui launch --once WxH [--client <tty>] [--pane <%id>] [--pick-space] [--plain]
//! ```
//!
//! - **Keys** ([`view::Launcher::key`]): ⏎ picks the selected agent, a
//!   letter that agent at once (either case); ↑/↓, C-p/C-n, Home/End and the
//!   wheel move; Esc, q, C-c, ⌥W and the ✕ close. Nothing cycles. ⌘E again
//!   cannot reach it: tmux drops a user key (User27) inside a popup, so the
//!   claude double-tap is ⌘E ⏎.
//! - **⌘E** (no flag): the agent in a new tab beside this one, in its
//!   folder, as ⌘R (`agent-launch.sh go LETTER CLIENT PANE`). The header
//!   says where: `here  2  notes`.
//! - **⌘⇧E** (`--pick-space`): the agent, then ⌘⇧R's own space picker
//!   (`agent-launch.sh pick --after <this pid> LETTER CLIENT PANE`: space.sh
//!   pick-tab agent with `SPACE_LAUNCH=<id>`, the same list, typed folders
//!   and ^N). A second popup cannot open while this one is up, so the script
//!   waits for this process to be gone first.
//! - **Data**: ONE tmux call ([`QUERY`]): the client's row (its session,
//!   its pane, the global `@launch_agents` and `@launch_sh`) and every
//!   session's ⌘N, name, hidden flag and colour (for the header).
//!   `@launch_agents` is agent-launch.sh's installed set (`refresh`:
//!   `c:claude:claude u:cursor:cursor-agent …`, its table's order), so the
//!   script stays the one table of agents and the one detector.
//! - **Launch**: the popup hands the command to the server (`run-shell -b`)
//!   and exits, so the birth, `@agent_harness` and the hookless agents stay
//!   in the script. An agent that vanished since the last refresh: the
//!   script says "launch: X not installed" (notify.sh) and records the set
//!   again.
//!
//! `--once` renders one frame of the live server to stdout (truecolour SGR,
//! or `--plain` with each logo as `[I]`) and exits; nothing is launched.
//! `AGENT_UI_LAUNCH_TRACE=<file>` appends `ready` there after the first
//! frame (tests/test_agent_launch.py waits on it before typing).

pub mod view;

use self::view::{render, Act, Agent, Here, Launcher};
use crate::menu::keys::{KeyReader, ESC_WAIT};
use crate::tmux::Tmux;
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;
use std::io::{self, BufWriter, Read, Write};
use std::process::ExitCode;
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::time::Duration;

const USAGE: &str = "usage: agent-ui launch --client <tty> [--pane <%id>] [--pick-space]
       agent-ui launch --once WxH [--client <tty>] [--pane <%id>] [--pick-space] [--plain]";

/// The popup's width (spaces.conf's User27 / User28 bindings ask for this
/// many columns, or the whole client when it is narrower). Its height is
/// [`view::height_for`] the installed count (`@launch_count`).
pub const POPUP_W: u16 = 46;
/// How often the loop wakes with nothing typed (to follow a resize).
const TICK: Duration = Duration::from_millis(250);
/// A file the popup appends `ready` to once its first frame is up (the
/// tests' cue that keys now reach it, not the pane).
pub const TRACE_ENV: &str = "AGENT_UI_LAUNCH_TRACE";
const US: char = '\x1f';

#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Args {
    pub client: Option<String>,
    pub pane: Option<String>,
    pub once: Option<(u16, u16)>,
    pub plain: bool,
    /// ⌘⇧E: the chosen agent goes to ⌘⇧R's space picker.
    pub pick_space: bool,
}

pub fn parse_args(args: &[String]) -> Result<Args, String> {
    let mut a = Args::default();
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let mut val = || it.next().cloned().ok_or_else(|| format!("{arg} needs a value"));
        match arg.as_str() {
            "--client" => a.client = Some(val()?),
            "--pane" => a.pane = Some(val()?),
            "--once" => {
                let v = val()?;
                let (w, h) = v.split_once('x').ok_or_else(|| format!("--once wants WxH, not {v:?}"))?;
                let n = |s: &str| s.parse::<u16>().map_err(|_| format!("bad size {v:?}"));
                a.once = Some((n(w)?, n(h)?));
            }
            "--plain" => a.plain = true,
            "--pick-space" => a.pick_space = true,
            _ => return Err(format!("unknown argument {arg:?}")),
        }
    }
    Ok(a)
}

/// The one call: the clients' rows (`C`), then every session's (`S`).
pub const QUERY: [&str; 7] = [
    "list-clients",
    "-F",
    "C\x1f#{client_tty}\x1f#{session_id}\x1f#{pane_id}\x1f#{@launch_agents}\x1f#{@launch_sh}",
    ";",
    "list-sessions",
    "-F",
    "S\x1f#{@space_index}\x1f#{session_id}\x1f#{session_name}\x1f#{@space_hidden}\x1f#{@space_color}",
];

/// What the popup needs, from [`QUERY`]'s output.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct State {
    pub agents: Vec<Agent>,
    /// The client's session, for the header.
    pub here: Option<Here>,
    /// The client's pane: the new tab goes beside its window.
    pub pane: String,
    /// `@launch_sh`: agent-launch.sh as a command line.
    pub launch_sh: String,
}

/// `@launch_agents` → the agents, in order. Each word is
/// `letter:id:command` (`letter:id` from an older script: the command is
/// the id); a word without a lowercase letter and an id is skipped, and so
/// is a letter already taken.
pub fn parse_agents(raw: &str) -> Vec<Agent> {
    let mut out: Vec<Agent> = Vec::new();
    for w in raw.split_whitespace() {
        let mut p = w.splitn(3, ':');
        let (Some(k), Some(id)) = (p.next(), p.next()) else { continue };
        let mut ks = k.chars();
        let Some(letter) = ks.next().filter(|c| c.is_ascii_lowercase() && ks.next().is_none()) else { continue };
        if id.is_empty() || out.iter().any(|a| a.letter == letter) {
            continue;
        }
        let command = p.next().filter(|c| !c.is_empty()).unwrap_or(id);
        out.push(Agent { letter, id: id.into(), command: command.into() });
    }
    out
}

/// `#rrggbb` → its colour.
fn hex(s: &str) -> Option<Color> {
    let h = s.trim().strip_prefix('#').filter(|h| h.len() == 6 && h.is_ascii())?;
    let n = |i: usize| u8::from_str_radix(&h[i..i + 2], 16).ok();
    Some(Color::Rgb(n(0)?, n(2)?, n(4)?))
}

/// [`QUERY`]'s output → the state, for `client` (its tty; the first client
/// when None or not found) and `pane` (the binding's `#{pane_id}`; the
/// client's own when None). `here` is the client's session: its ⌘N and
/// colour when it is a space (not hidden, numbered), else just its name.
pub fn parse_state(text: &str, client: Option<&str>, pane: Option<&str>) -> State {
    let mut clients: Vec<Vec<&str>> = Vec::new();
    let mut sessions: Vec<Vec<&str>> = Vec::new();
    for line in text.lines() {
        let f: Vec<&str> = line.split(US).collect();
        match f.first() {
            Some(&"C") if f.len() >= 6 => clients.push(f),
            Some(&"S") if f.len() >= 6 => sessions.push(f),
            _ => {}
        }
    }
    let me = client.and_then(|c| clients.iter().find(|f| f[1] == c)).or(clients.first());
    let (cur, mine_pane, agents, launch_sh) = match me {
        Some(f) => (f[2], f[3], f[4], f[5]),
        None => ("", "", "", ""),
    };
    let here = sessions.iter().find(|f| !cur.is_empty() && f[2] == cur).map(|f| {
        let index = if f[4] == "1" { None } else { f[1].trim().parse::<u32>().ok() };
        Here { name: f[3].into(), index, color: index.and_then(|_| hex(f[5])) }
    });
    State {
        agents: parse_agents(agents),
        here,
        pane: pane.filter(|p| !p.is_empty()).unwrap_or(mine_pane).into(),
        launch_sh: launch_sh.into(),
    }
}

/// Single-quoted for sh.
fn q(s: &str) -> String {
    format!("'{}'", s.replace('\'', r"'\''"))
}

/// The `run-shell -b` command for `act`: `agent-launch.sh go LETTER CLIENT
/// PANE` (⌘E), or `agent-launch.sh pick --after PID LETTER CLIENT PANE`
/// (⌘⇧E: `pid` is this popup's process, which must be gone before the
/// picker's popup can open). `launch_sh` is a command line (`bash /path` in
/// the tests); unset, the installed script. None for Close.
pub fn launch_command(launch_sh: &str, act: &Act, client: &str, pane: &str, pid: u32) -> Option<String> {
    let sh = if launch_sh.trim().is_empty() {
        let home = std::env::var("HOME").unwrap_or_default();
        format!("{}/.config/tmux/scripts/agent-launch.sh", home)
    } else {
        launch_sh.to_string()
    };
    let verb = match act {
        Act::Launch(l) => format!("go {l}"),
        Act::Pick(l) => format!("pick --after {pid} {l}"),
        Act::Close => return None,
    };
    // run-shell expands formats in its command: a literal `#` stays one.
    Some(format!("{sh} {verb} {} {}", q(client), q(pane)).replace('#', "##"))
}

pub fn run(tmux: Tmux, args: &[String]) -> ExitCode {
    let a = match parse_args(args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("agent-ui launch: {e}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let text = match tmux.run(&QUERY) {
        Ok(o) if o.ok() => o.stdout,
        _ => String::new(),
    };
    let st = parse_state(&text, a.client.as_deref(), a.pane.as_deref());
    let mut l = Launcher::new(st.agents.clone(), st.here.clone(), a.pick_space);
    if let Some((w, h)) = a.once {
        let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
        render(&mut l, &mut buf);
        let lines = crate::menu::render::dump(&buf, !a.plain);
        let lines = if a.plain { view::initials(&lines) } else { lines };
        let mut out = io::stdout().lock();
        for line in lines {
            let _ = writeln!(out, "{line}");
        }
        return ExitCode::SUCCESS;
    }
    match interactive(&mut l) {
        Ok(Some(act)) => {
            let client = a.client.clone().unwrap_or_default();
            if let Some(cmd) = launch_command(&st.launch_sh, &act, &client, &st.pane, std::process::id()) {
                tmux.run_bg(&cmd);
            }
            ExitCode::SUCCESS
        }
        Ok(None) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("agent-ui launch: {e}");
            ExitCode::from(1)
        }
    }
}

/// Raw mode, the alternate screen and SGR mouse (1000 + 1006: clicks and
/// the wheel, never motion), all undone on drop.
struct TermGuard;

impl TermGuard {
    fn new() -> io::Result<TermGuard> {
        crossterm::terminal::enable_raw_mode()?;
        let mut o = io::stdout().lock();
        o.write_all(b"\x1b[?1049h\x1b[?25l\x1b[2J\x1b[?1000h\x1b[?1006h")?;
        o.flush()?;
        Ok(TermGuard)
    }
}

impl Drop for TermGuard {
    fn drop(&mut self) {
        let mut o = io::stdout().lock();
        let _ = o.write_all(b"\x1b[?1006l\x1b[?1000l\x1b[0m\x1b[?25h\x1b[?1049l");
        let _ = o.flush();
        let _ = crossterm::terminal::disable_raw_mode();
    }
}

enum Msg {
    Input(Vec<u8>),
    Eof,
}

fn spawn_reader(tx: Sender<Msg>) {
    std::thread::spawn(move || {
        let mut stdin = io::stdin().lock();
        let mut buf = [0u8; 4096];
        loop {
            match stdin.read(&mut buf) {
                Ok(0) => break,
                Ok(n) => {
                    if tx.send(Msg::Input(buf[..n].to_vec())).is_err() {
                        return;
                    }
                }
                Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
                Err(_) => break,
            }
        }
        let _ = tx.send(Msg::Eof);
    });
}

type Term = ratatui::Terminal<ratatui::backend::CrosstermBackend<BufWriter<io::Stdout>>>;

/// Render; write only when the frame changed.
fn draw(term: &mut Term, l: &mut Launcher, last: &mut Option<Buffer>) -> io::Result<()> {
    let (w, h) = crossterm::terminal::size()?;
    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
    render(l, &mut buf);
    if last.as_ref() == Some(&buf) {
        return Ok(());
    }
    term.draw(|f| {
        let b = f.buffer_mut();
        if b.area == buf.area {
            b.content.clone_from(&buf.content);
        } else {
            render(l, b);
        }
    })?;
    *last = Some(buf);
    Ok(())
}

/// Until a key closes or launches (None: stdin closed).
fn interactive(l: &mut Launcher) -> io::Result<Option<Act>> {
    let _guard = TermGuard::new()?;
    let (tx, rx) = mpsc::channel::<Msg>();
    spawn_reader(tx);
    let mut term: Term =
        ratatui::Terminal::new(ratatui::backend::CrosstermBackend::new(BufWriter::with_capacity(1 << 16, io::stdout())))?;
    let mut reader = KeyReader::new();
    let mut last: Option<Buffer> = None;
    let mut trace = std::env::var_os(TRACE_ENV).filter(|p| !p.is_empty());
    loop {
        draw(&mut term, l, &mut last)?;
        if let Some(p) = trace.take() {
            let _ = std::fs::OpenOptions::new().create(true).append(true).open(p).and_then(|mut f| f.write_all(b"ready\n"));
        }
        let bytes = match rx.recv_timeout(TICK) {
            Ok(Msg::Input(b)) => b,
            Ok(Msg::Eof) | Err(RecvTimeoutError::Disconnected) => return Ok(None),
            Err(RecvTimeoutError::Timeout) => continue,
        };
        let mut keys = reader.feed(&bytes);
        // A read that ended inside a sequence (or on a bare ESC) waits
        // ESC_WAIT for the rest; only silence makes a lone ESC "esc".
        let mut eof = false;
        while reader.pending() {
            match rx.recv_timeout(ESC_WAIT) {
                Ok(Msg::Input(b)) => keys.extend(reader.feed(&b)),
                Ok(Msg::Eof) => {
                    keys.extend(reader.flush());
                    eof = true;
                }
                Err(_) => keys.extend(reader.flush()),
            }
        }
        while let Ok(m) = rx.try_recv() {
            match m {
                Msg::Input(b) => keys.extend(reader.feed(&b)),
                Msg::Eof => eof = true,
            }
        }
        if let Some(act) = l.handle(&keys) {
            return Ok(Some(act));
        }
        if eof {
            return Ok(None);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn s(v: &[&str]) -> Vec<String> {
        v.iter().map(|x| x.to_string()).collect()
    }

    #[test]
    fn args() {
        let a = parse_args(&s(&["--client", "/dev/ttys004", "--pane", "%12"])).unwrap();
        assert_eq!((a.client.as_deref(), a.pane.as_deref(), a.once), (Some("/dev/ttys004"), Some("%12"), None));
        assert!(!a.pick_space);
        let a = parse_args(&s(&["--once", "60x16", "--plain", "--pick-space"])).unwrap();
        assert_eq!((a.once, a.plain, a.pick_space), (Some((60, 16)), true, true));
        assert!(parse_args(&s(&["--once", "60"])).is_err());
        assert!(parse_args(&s(&["--pane"])).is_err());
        assert!(parse_args(&s(&["--bogus"])).is_err());
    }

    #[test]
    fn agents_from_the_scripts_option() {
        let v = parse_agents("c:claude:claude u:cursor:cursor-agent x:codex junk Q:grok m: gg:grok :x c:again d:aider:");
        let got: Vec<(char, &str, &str)> = v.iter().map(|a| (a.letter, a.id.as_str(), a.command.as_str())).collect();
        assert_eq!(got, [('c', "claude", "claude"), ('u', "cursor", "cursor-agent"), ('x', "codex", "codex"),
            ('d', "aider", "aider")]);
        assert!(parse_agents("").is_empty());
    }

    fn c(tty: &str, sid: &str, pane: &str, agents: &str) -> String {
        format!("C\x1f{tty}\x1f{sid}\x1f{pane}\x1f{agents}\x1fbash /x/agent-launch.sh")
    }

    fn sess(idx: &str, sid: &str, name: &str, hidden: &str, col: &str) -> String {
        format!("S\x1f{idx}\x1f{sid}\x1f{name}\x1f{hidden}\x1f{col}")
    }

    #[test]
    fn state_from_one_call() {
        let text = [
            c("/dev/ttys001", "$0", "%1", "c:claude:claude"),
            c("/dev/ttys004", "$3", "%7", "c:claude:claude x:codex:codex"),
            sess("3", "$5", "cua-notch", "", "#94e2d5"),
            sess("1", "$0", "main", "", "#b4befe"),
            sess("", "$9", "agents", "1", ""),
            sess("2", "$3", "notes", "", "junk"),
            sess("4", "$6", "stash", "1", ""),
        ]
        .join("\n");
        let st = parse_state(&text, Some("/dev/ttys004"), None);
        assert_eq!(st.agents.len(), 2);
        assert_eq!((st.pane.as_str(), st.launch_sh.as_str()), ("%7", "bash /x/agent-launch.sh"));
        assert_eq!(st.here, Some(Here { name: "notes".into(), index: Some(2), color: None })); // "junk" is no colour
        let st = parse_state(&text, Some("/dev/ttys001"), None);
        assert_eq!(st.here, Some(Here { name: "main".into(), index: Some(1), color: Some(Color::Rgb(0xb4, 0xbe, 0xfe)) }));
        // The binding's pane wins; an unknown client is the first one.
        let st = parse_state(&text, Some("/dev/nope"), Some("%42"));
        assert_eq!((st.pane.as_str(), st.agents.len()), ("%42", 1));
        // A client in a hidden session: its name, no number, no colour.
        let text = [c("/dev/ttys001", "$9", "%3", ""), sess("1", "$0", "main", "", ""), sess("7", "$9", "agents", "1", "#94e2d5")]
            .join("\n");
        let st = parse_state(&text, Some("/dev/ttys001"), None);
        assert_eq!(st.here, Some(Here { name: "agents".into(), index: None, color: None }));
        // No server: nothing at all.
        assert_eq!(parse_state("", None, None), State::default());
    }

    #[test]
    fn the_launch_command() {
        let cmd = |sh: &str, act: Act, client: &str| launch_command(sh, &act, client, "%7", 4242);
        assert_eq!(cmd("bash /x/a.sh", Act::Launch('x'), "/dev/ttys004").as_deref(),
            Some("bash /x/a.sh go x '/dev/ttys004' '%7'"));
        // ⌘⇧E: the picker, once this popup (pid 4242) is gone.
        assert_eq!(cmd("/x/a.sh", Act::Pick('c'), "/dev/ttys004").as_deref(),
            Some("/x/a.sh pick --after 4242 c '/dev/ttys004' '%7'"));
        assert_eq!(cmd("/x/a#b.sh", Act::Launch('c'), "it's").as_deref(), Some("/x/a##b.sh go c 'it'\\''s' '%7'"));
        assert_eq!(cmd("/x/a.sh", Act::Close, ""), None);
        assert!(cmd("", Act::Launch('c'), "").unwrap().ends_with("/.config/tmux/scripts/agent-launch.sh go c '' '%7'"));
    }

    #[test]
    fn query_is_one_call() {
        assert_eq!(QUERY.iter().filter(|a| **a == ";").count(), 1);
        assert!(QUERY[2].contains("#{@launch_agents}") && QUERY[2].contains("#{@launch_sh}"));
        assert!(QUERY[6].contains("#{@space_index}") && QUERY[6].contains("#{@space_hidden}"));
    }
}
