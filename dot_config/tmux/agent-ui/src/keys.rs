//! `agent-ui keys`: the key help, `prefix ?`. Every key of the setup (the
//! WezTerm ⌘ chords, the root ⌥ keys, the ⌥W jump table, the prefix keys,
//! the agent menu's own keys) from ONE catalog, `keys.tsv`, in a popup.
//!
//! ```text
//! agent-ui keys [--client <tty>] [--file <keys.tsv>]
//! agent-ui keys --once WxH [--query q] [--ansi] [--file <keys.tsv>]
//! ```
//!
//! The catalog is read from `~/.config/tmux/keys.tsv` (chezmoi's
//! `dot_config/tmux/keys.tsv`) at every open, so an added row shows at once;
//! a missing or empty file falls back to the copy built into the binary
//! ([`catalog::EMBEDDED`]). Typing filters (a key, spelled out or not, or
//! words of the description); Backspace edits; ↑/↓, C-p/C-n, PgUp/PgDn,
//! Home/End and the wheel scroll; Esc clears the filter, then closes; `?`
//! or `q` close while the filter is empty; so does a click on the ✕.
//!
//! It reads no tmux state and runs no tmux command: `--client` is accepted
//! for symmetry with the other popups and ignored. The pieces: [`catalog`]
//! (parse and filter) and [`view`] (state, layout, one frame); this file is
//! the I/O, and [`popup_command`], the popup for a given client.

pub mod catalog;
pub mod view;

use self::view::{render, Help};
use crate::menu::keys::{KeyReader, ESC_WAIT};
use crate::tmux::{Socket, Tmux};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use std::io::{self, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::time::Duration;

const USAGE: &str = "usage: agent-ui keys [--client <tty>] [--file <keys.tsv>]
       agent-ui keys --once WxH [--query q] [--ansi] [--file <keys.tsv>]";

/// The popup's widest: the binding asks for this many columns, or the
/// whole client when it is narrower. Two columns from about 94.
pub const POPUP_W: u16 = 124;
/// The popup's height (a tmux size).
pub const POPUP_H: &str = "85%";
/// How often the loop wakes with nothing typed (to follow a resize).
const TICK: Duration = Duration::from_millis(250);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    pub client: Option<String>,
    pub file: Option<PathBuf>,
    pub once: Option<(u16, u16)>,
    pub query: String,
    pub ansi: bool,
}

pub fn parse_args(args: &[String]) -> Result<Args, String> {
    let mut a = Args { client: None, file: None, once: None, query: String::new(), ansi: false };
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let mut val = || it.next().cloned().ok_or_else(|| format!("{arg} needs a value"));
        match arg.as_str() {
            "--client" => a.client = Some(val()?),
            "--file" => a.file = Some(PathBuf::from(val()?)),
            "--query" => a.query = val()?,
            "--once" => {
                let v = val()?;
                let (w, h) = v.split_once('x').ok_or_else(|| format!("--once wants WxH, not {v:?}"))?;
                let n = |s: &str| s.parse::<u16>().map_err(|_| format!("bad size {v:?}"));
                a.once = Some((n(w)?, n(h)?));
            }
            "--ansi" => a.ansi = true,
            _ => return Err(format!("unknown argument {arg:?}")),
        }
    }
    Ok(a)
}

/// Where the catalog lives: `$HOME/.config/tmux/keys.tsv`.
pub fn default_path() -> Option<PathBuf> {
    std::env::var_os("HOME").map(|h| PathBuf::from(h).join(".config/tmux/keys.tsv"))
}

/// The catalog from `path` (or [`default_path`]); the built-in copy when
/// that cannot be read or has no rows.
pub fn load(path: Option<&Path>) -> Vec<catalog::Section> {
    let p = path.map(Path::to_path_buf).or_else(default_path);
    if let Some(text) = p.and_then(|p| std::fs::read_to_string(p).ok()) {
        let s = catalog::parse(&text);
        if !s.is_empty() {
            return s;
        }
    }
    catalog::parse(catalog::EMBEDDED)
}

/// The popup's `-w` for a client this many columns wide: [`POPUP_W`], or
/// the whole client when narrower; unknown → 90%.
pub fn popup_width(client_cols: Option<u16>) -> String {
    match client_cols {
        Some(c) if c > 0 => c.min(POPUP_W).to_string(),
        _ => "90%".into(),
    }
}

/// The key help as a popup on `client`, from outside tmux's key bindings
/// (the sidebar): what `prefix ?` runs, as one `tmux display-popup -c`.
/// `exe` is this binary; it is pointed at the same server. `-B`: it draws
/// its own border. `-x`: centred on the whole WezTerm row (tmux.conf.tmpl's
/// `@popup_x`, else tmux's centre). Spawn it detached and reap it: it
/// returns when the popup closes.
pub fn popup_command(tmux: &Tmux, client: &str, exe: &Path, client_cols: Option<u16>) -> Command {
    let mut c = tmux.command();
    c.args(["display-popup", "-c", client, "-E", "-B", "-w", &popup_width(client_cols), "-h", POPUP_H, "-x",
        crate::sidebar::status::POPUP_X]);
    c.arg(exe);
    if let Some(Socket::Name(s) | Socket::Path(s)) = &tmux.socket {
        c.args(["--socket", s]);
    }
    c.args(["keys", "--client", client]);
    c
}

pub fn run(_tmux: Tmux, args: &[String]) -> ExitCode {
    let a = match parse_args(args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("agent-ui keys: {e}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let mut help = Help::new(load(a.file.as_deref()));
    if let Some((w, h)) = a.once {
        help.query = a.query.clone();
        let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
        render(&mut help, &mut buf);
        let mut out = io::stdout().lock();
        for l in crate::menu::render::dump(&buf, a.ansi) {
            let _ = writeln!(out, "{l}");
        }
        return ExitCode::SUCCESS;
    }
    match interactive(help) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("agent-ui keys: {e}");
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
fn draw(term: &mut Term, help: &mut Help, last: &mut Option<Buffer>) -> io::Result<()> {
    let (w, h) = crossterm::terminal::size()?;
    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
    render(help, &mut buf);
    if last.as_ref() == Some(&buf) {
        return Ok(());
    }
    term.draw(|f| {
        let b = f.buffer_mut();
        if b.area == buf.area {
            b.content.clone_from(&buf.content);
        } else {
            render(help, b);
        }
    })?;
    *last = Some(buf);
    Ok(())
}

fn interactive(mut help: Help) -> io::Result<()> {
    let _guard = TermGuard::new()?;
    let (tx, rx) = mpsc::channel::<Msg>();
    spawn_reader(tx);
    let mut term: Term =
        ratatui::Terminal::new(ratatui::backend::CrosstermBackend::new(BufWriter::with_capacity(1 << 16, io::stdout())))?;
    let mut reader = KeyReader::new();
    let mut last: Option<Buffer> = None;
    loop {
        draw(&mut term, &mut help, &mut last)?;
        let bytes = match rx.recv_timeout(TICK) {
            Ok(Msg::Input(b)) => b,
            Ok(Msg::Eof) | Err(RecvTimeoutError::Disconnected) => return Ok(()),
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
        // Everything else already queued, before the next draw.
        while let Ok(m) = rx.try_recv() {
            match m {
                Msg::Input(b) => keys.extend(reader.feed(&b)),
                Msg::Eof => eof = true,
            }
        }
        if help.handle(&keys) || eof {
            return Ok(());
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
        let a = parse_args(&s(&["--client", "/dev/ttys004"])).unwrap();
        assert_eq!((a.client.as_deref(), a.once, a.ansi), (Some("/dev/ttys004"), None, false));
        let a = parse_args(&s(&["--once", "100x30", "--query", "park", "--ansi", "--file", "/x/k.tsv"])).unwrap();
        assert_eq!((a.once, a.query.as_str(), a.ansi), (Some((100, 30)), "park", true));
        assert_eq!(a.file.as_deref(), Some(Path::new("/x/k.tsv")));
        assert!(parse_args(&s(&["--once", "100"])).is_err());
        assert!(parse_args(&s(&["--client"])).is_err());
        assert!(parse_args(&s(&["--bogus"])).is_err());
    }

    #[test]
    fn load_falls_back_to_the_built_in_copy() {
        let dir = std::env::temp_dir().join(format!("agent-ui-keys-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let mine = dir.join("keys.tsv");
        std::fs::write(&mine, "Mine\t⌘K\tMy key\n").unwrap();
        let s = load(Some(&mine));
        assert_eq!((s.len(), s[0].name.as_str()), (1, "Mine"));
        // Comments only, or no file: the built-in catalog.
        std::fs::write(&mine, "# nothing\n").unwrap();
        let builtin = catalog::parse(catalog::EMBEDDED);
        assert_eq!(load(Some(&mine)), builtin);
        assert_eq!(load(Some(&dir.join("missing.tsv"))), builtin);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn popup_widths_and_command() {
        assert_eq!(popup_width(Some(200)), "124");
        assert_eq!(popup_width(Some(80)), "80");
        assert_eq!((popup_width(Some(0)), popup_width(None)), ("90%".to_string(), "90%".to_string()));
        let t = Tmux::with_socket(Some(Socket::Name("priv".into())));
        let c = popup_command(&t, "/dev/ttys999", Path::new("/x/agent-ui"), Some(100));
        let argv: Vec<String> = c.get_args().map(|a| a.to_string_lossy().into_owned()).collect();
        let tail: Vec<&str> = argv.iter().map(String::as_str).skip_while(|a| *a != "display-popup").collect();
        assert_eq!(tail, ["display-popup", "-c", "/dev/ttys999", "-E", "-B", "-w", "100", "-h", "85%", "-x",
            crate::sidebar::status::POPUP_X, "/x/agent-ui", "--socket", "priv", "keys", "--client", "/dev/ttys999"]);
    }
}
