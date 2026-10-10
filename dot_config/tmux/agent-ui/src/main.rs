//! `agent-ui`: the agent sidebar (CMD+B) and Option-W menu, plus the core's
//! debugging hooks.
//!
//! ```text
//! agent-ui [--socket <name|path>] dump [--client <tty>] [--json] [--log <n>]
//! agent-ui [--socket <name|path>] needs
//! agent-ui [--socket <name|path>] sidebar ...   (see sidebar.rs)
//! agent-ui [--socket <name|path>] menu ...      (see menu.rs)
//! agent-ui [--socket <name|path>] sessions ...  (see sessions.rs; prefix a)
//! ```
//!
//! `needs` prints exactly what `agent-jump.sh list` prints, from the same
//! tmux rows; the parity test diffs the two.

use agent_ui::{model, text, Core, Socket, Tmux};
use std::io::Write;
use std::process::ExitCode;

const USAGE: &str = "usage: agent-ui [--socket <name|path>] dump [--client <tty>] [--json] [--log <n>]
       agent-ui [--socket <name|path>] needs
       agent-ui sidebar [--client <tty>] | --once <W>x<H>
       agent-ui menu --client <tty> [--tab <tab>] | --once <W>x<H>
       agent-ui sessions --client <tty> | --once <W>x<H> [--client <tty>] [--query <q>] [--ansi]";

fn arg_after<'a>(args: &'a [String], flag: &str) -> Option<&'a str> {
    let i = args.iter().position(|a| a == flag)?;
    args.get(i + 1).map(String::as_str)
}

fn main() -> ExitCode {
    let mut args: Vec<String> = std::env::args().skip(1).collect();
    let mut tmux = Tmux::from_env();
    if let Some(i) = args.iter().position(|a| a == "--socket" || a == "-L") {
        let Some(v) = args.get(i + 1).cloned() else {
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        };
        tmux = Tmux::with_socket(Socket::parse(&v));
        args.drain(i..i + 2);
    }
    let Some(cmd) = args.first().cloned() else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    let rest = &args[1..];
    match cmd.as_str() {
        "needs" => needs(&tmux),
        "dump" => dump(tmux, rest),
        "sidebar" => agent_ui::sidebar::run(tmux, rest),
        "menu" => agent_ui::menu::run(tmux, rest),
        "sessions" => agent_ui::sessions::run(tmux, rest),
        "-h" | "--help" | "help" => {
            println!("{USAGE}");
            ExitCode::SUCCESS
        }
        _ => {
            eprintln!("{USAGE}");
            ExitCode::from(2)
        }
    }
}

/// agent-jump.sh `list`'s output: `win\tsession\tindex\tstate\tsince\tlabel`.
fn needs(tmux: &Tmux) -> ExitCode {
    let snap = tmux.snapshot();
    let coll = agent_ui::Collator::from_env();
    let mut out = std::io::stdout().lock();
    for r in model::needs_order(&snap.windows, &coll) {
        let _ = writeln!(out, "{}", r.list_line());
    }
    ExitCode::SUCCESS
}

fn dump(tmux: Tmux, rest: &[String]) -> ExitCode {
    let client = arg_after(rest, "--client");
    let mut core = Core::new(tmux);
    if let Some(n) = arg_after(rest, "--log").and_then(|n| n.parse().ok()) {
        core.log_limit = n;
    }
    core.git.branch_wait = std::time::Duration::from_millis(500);
    let _ = core.refresh(client);
    // One-shot: let the background git status land (bounded), then rebuild.
    core.git.settle(std::time::Duration::from_secs(4));
    let view = core.rebuild(client);
    let mut out = std::io::stdout().lock();
    if rest.iter().any(|a| a == "--json") {
        let _ = writeln!(out, "{}", view.to_json().to_json_pretty());
        return ExitCode::SUCCESS;
    }
    let c: Vec<String> = view.counts.nonzero().iter().map(|(k, n)| format!("{}{n}", k.glyph())).collect();
    let _ = writeln!(out, "agents  {}   blink={} locale={}", c.join(" "), view.blink, view.locale);
    if let Some(cl) = &view.client {
        let _ = writeln!(out, "client  {cl} on {:?} {}", view.cur_win, if view.client_gone { "(gone)" } else { "" });
    }
    let _ = writeln!(out, "\nneeds you ({})", view.needs.len());
    for n in &view.needs {
        let a = &n.agent;
        let _ = writeln!(out, "  {} {:<28} {:<6} {} {}  {}", a.glyph, text::clip(&a.title, 28), a.age, n.reason_word,
            text::clip(&n.reason, 30), a.place());
    }
    let _ = writeln!(out, "\nspaces");
    for s in &view.spaces {
        let c: Vec<String> = s.counts.nonzero().iter().map(|(k, n)| format!("{}{n}", k.glyph())).collect();
        let git = s.git.as_ref().map(|g| format!(" ↑{} ↓{} ✎{}", g.ahead, g.behind, g.dirty())).unwrap_or_default();
        let _ = writeln!(out, "  {} {:<16} {:<14} ⎇ {}{}  {}", if s.is_current { "●" } else { "○" }, s.name,
            c.join(" "), s.branch, git, s.path_short);
    }
    let _ = writeln!(out, "\nagents");
    for s in &view.spaces {
        if s.agents.is_empty() {
            continue;
        }
        let _ = writeln!(out, "  ── {}", s.name);
        for a in &s.agents {
            let _ = writeln!(out, "    {} {:>3} {:<34} {:>4} {}{}", a.glyph, a.index, text::clip(&a.title, 34), a.age,
                a.color.unwrap_or("-"), if a.is_current { "  (here)" } else { "" });
        }
    }
    let _ = writeln!(out, "\nparked ({})", view.parked.len());
    for p in &view.parked {
        let _ = writeln!(out, "  {:<34} {:<10} {:>4} {}", text::clip(&p.agent.title, 34), p.origin, p.age, p.state_words);
    }
    let _ = writeln!(out, "\nlog ({})", view.log.len());
    for e in view.log.iter().take(12) {
        let _ = writeln!(out, "  {} {} {}  {}", e.time, e.glyph, e.event.title, e.event.state);
    }
    ExitCode::SUCCESS
}
