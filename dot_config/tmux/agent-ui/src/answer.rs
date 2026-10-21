//! `agent-ui answer WINDOW_ID allow|deny [--client <tty>]`: answer an
//! agent's tool-permission prompt without going to its tab. The sidebar's
//! `y allow` / `n deny` chips on a NEEDS YOU card and the menu's Space y /
//! Space n run the same [`answer`].
//!
//! Nothing is typed on a guess. The window must be a Claude agent whose last
//! event was a permission gate (`@agent_kind` claude, `@agent_detail_kind`
//! perm), and its pane (the agent's own, [`Actions::agent_pane`]) must show
//! Claude Code's permission dialog RIGHT NOW: one `capture-pane -p` of the
//! visible screen, read by [`detect`]. A prompt already answered, an
//! AskUserQuestion, or a layout this does not know is refused, and nothing
//! is sent. The dialog as Claude Code 2.1.306 draws it (captured live):
//!
//! ```text
//!  Do you want to proceed?            (or "Do you want to create x.txt?", …)
//!  ❯ 1. Yes
//!    2. Yes, and always allow access to … from this project
//!    3. No
//!
//!  Esc to cancel · Tab to amend
//! ```
//!
//! - allow: the digit of the option that reads exactly `Yes` (the single-use
//!   yes, never "Yes, and don't ask again"), which Claude's select takes as
//!   a pick on its own (no ⏎).
//! - deny: Esc, the dialog's own cancel (the `No` option's).
//!
//! Both only while the select's `❯` is on that `Yes` line: moved onto an
//! editable row ("Yes, and don't ask again for: …"), a digit would type
//! into it.
//!
//! Which prompt: a click or key names the prompt it saw ([`prompt_key`]:
//! the window's `@agent_since` and detail as drawn). A window whose current
//! prompt is another one (answered, then asked again) is refused, so a
//! stale frame can never answer the NEXT prompt.
//!
//! Check and send are one step: the capture rides in the same tmux call as
//! the pane's `cursor_y`, `history_size`, `pane_in_mode`, `pane_mode` and
//! `synchronize-panes` ([`STATE_FMT`]), and the keys go through ONE
//! `if-shell -F` whose condition is those same values ([`plan`]): a pane
//! that redrew or scrolled in between gets nothing. Keys go to the pane by
//! id (`send-keys -t %N`), never `session:index`. A pane left in copy mode
//! (or view mode: scrolled back, nobody there) captures its live screen but
//! would take the keys as copy-mode commands, so the same command list
//! cancels the mode first (only the scroll position is lost); any other
//! pane mode, and a window with `synchronize-panes` on (the keys would go
//! to every pane in it), is refused.
//!
//! After a send the window is discharged exactly as if Mack had looked at
//! it: agent-tab-indicator.sh's own `clear-current <window>` (the focus
//! hook's entry point; needs-input → idle with `@agent_pending` stamped),
//! through `run-shell` so it runs with the server's `$TMUX`. Without it a
//! denied prompt (no PostToolUse follows) kept its card, and a click on it
//! during a long allowed tool would answer the next prompt.
//! Codex draws its own approval overlay ("Would you like to run the
//! following command?" `› 1. Yes, proceed`), not verified on a live pane
//! here, so a Codex agent is refused (as is OpenCode): go to it.
//!
//! The command line refuses through notify.sh (a toast in the sidebar, else
//! tmux's message; `$NOTIFY_SH` names another, as agent-jump.sh), exit 1;
//! the sidebar and the menu say the refusal on their own message line.

use crate::actions::{sh_quote, Actions, PANE_GONE};
use crate::proc;
use crate::tmux::{Snapshot, Tmux, Window};
use crate::view::Agent;
use std::process::{Command, ExitCode};
use std::time::Duration;

const USAGE: &str = "usage: agent-ui answer <window_id> allow|deny [--client <tty>]";

/// Allow (the single-use Yes) or deny (Esc).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Verdict {
    Allow,
    Deny,
}

impl Verdict {
    pub fn parse(s: &str) -> Option<Verdict> {
        match s {
            "allow" => Some(Verdict::Allow),
            "deny" => Some(Verdict::Deny),
            _ => None,
        }
    }

    /// The past tense the message line says.
    fn done(self) -> &'static str {
        match self {
            Verdict::Allow => "allowed",
            Verdict::Deny => "denied",
        }
    }
}

/// A permission dialog [`detect`] read off the screen.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Dialog {
    /// The number of the option that reads exactly `Yes`.
    pub yes: u8,
    /// The number of the `No` option (always the last).
    pub no: u8,
}

/// Why [`detect`] saw no dialog to answer.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Refusal {
    /// No "Do you want to …" with options under it at the bottom of the
    /// screen: answered already, or never there.
    NoDialog,
    /// AskUserQuestion's picker (its own options, "Type something.", "Chat
    /// about this", "Enter to select"): a question, not a permission.
    Question,
    /// A dialog, but not one this knows how to answer (no plain `Yes`, no
    /// `No` last, options out of order, something under it).
    Unknown,
    /// The dialog, but the select's `❯` is not on its `Yes` line.
    Pointer,
}

impl Refusal {
    pub fn words(self) -> &'static str {
        match self {
            Refusal::NoDialog => "no permission prompt on screen",
            Refusal::Question => "that's a question, not a permission prompt",
            Refusal::Unknown => "can't read that prompt's options",
            Refusal::Pointer => "its cursor is off Yes · answer it in its tab",
        }
    }
}

/// A line without its frame: whitespace and an old-style box's `│` sides.
fn bare(l: &str) -> &str {
    l.trim().trim_matches('│').trim()
}

/// Claude Code's select pointer.
const POINTER: char = '❯';

/// `❯ 1. Yes` / `  3. No` → (3, "No", pointed): a numbered option, and
/// whether the select's pointer is on it.
fn option(l: &str) -> Option<(u8, &str, bool)> {
    let b = bare(l);
    let s = b.trim_start_matches(POINTER).trim_start();
    let (n, label) = s.split_once(". ")?;
    if n.len() != 1 || !n.as_bytes()[0].is_ascii_digit() {
        return None;
    }
    Some((n.parse().ok()?, label.trim(), b.starts_with(POINTER)))
}

/// Text only AskUserQuestion's picker draws.
const QUESTION_MARKS: [&str; 3] = ["Type something", "Chat about this", "Enter to select"];

/// The footer Claude Code draws under a permission dialog's options.
const FOOTER: &str = "Esc to cancel";

/// Read Claude Code's permission dialog off a `capture-pane -p` of the
/// visible screen. It must be the LAST thing drawn: the last line starting
/// "Do you want to", numbered options under it (1, 2, … in order) with one
/// that reads exactly `Yes` and a `No…` one last, then nothing but blank
/// lines, a wrapped option's indented continuation, and the `Esc to
/// cancel` footer. Anything else (the input box back under it, a picker's
/// marks) is a refusal; so is the select's `❯` anywhere but on `Yes`.
pub fn detect(capture: &str) -> Result<Dialog, Refusal> {
    let lines: Vec<&str> = capture.lines().collect();
    let q = lines.iter().rposition(|l| bare(l).starts_with("Do you want to ")).ok_or(Refusal::NoDialog)?;
    let body = &lines[q + 1..];
    if body.iter().any(|l| QUESTION_MARKS.iter().any(|m| l.contains(m))) {
        return Err(Refusal::Question);
    }
    let opts: Vec<(usize, u8, &str, bool)> =
        body.iter().enumerate().filter_map(|(i, l)| option(l).map(|(n, t, p)| (i, n, t, p))).collect();
    if opts.is_empty() {
        return Err(Refusal::NoDialog);
    }
    if opts.iter().enumerate().any(|(k, o)| usize::from(o.1) != k + 1) {
        return Err(Refusal::Unknown);
    }
    let yes_opt = opts.iter().find(|o| o.2 == "Yes").ok_or(Refusal::Unknown)?;
    let yes = yes_opt.1;
    let &(last_at, no, label, _) = opts.last().expect("not empty");
    let no_word = label == "No" || label.starts_with("No,") || label.starts_with("No ");
    if !no_word || no <= yes {
        return Err(Refusal::Unknown);
    }
    // Under the last option: its wrapped tail, blanks, the footer; nothing else.
    for l in &body[last_at + 1..] {
        let indented = l.starts_with("    ") && !bare(l).is_empty();
        let footer = bare(l).starts_with(FOOTER);
        if !(bare(l).is_empty() || indented || footer) {
            return Err(Refusal::Unknown);
        }
    }
    if !yes_opt.3 {
        return Err(Refusal::Pointer);
    }
    Ok(Dialog { yes, no })
}

/// What `send-keys` gets for `v`: the Yes option's digit, or Escape.
pub fn keys(d: Dialog, v: Verdict) -> Vec<String> {
    match v {
        Verdict::Allow => vec![d.yes.to_string()],
        Verdict::Deny => vec!["Escape".into()],
    }
}

/// The card of an agent a permission prompt is waiting on, that the sidebar
/// and the menu offer allow / deny for: needs-input, a `perm` detail, Claude.
pub fn answerable(a: &Agent) -> bool {
    a.state == "needs-input" && a.detail_kind == "perm" && a.kind == "claude"
}

/// The window's own refusal, before any pane is read: not an agent this can
/// answer for, or no permission gate as its last event.
fn refuse_window(w: &Window) -> Option<String> {
    match w.kind.as_str() {
        "claude" => {}
        "codex" => return Some(format!("{}: answer Codex prompts in its tab", w.label)),
        _ => return Some(format!("{}: not a Claude agent", w.label)),
    }
    // needs-input too: an answered (discharged) prompt is idle, so a second
    // click on its card can never reach the prompt after it.
    (w.detail_kind != "perm" || w.state != "needs-input").then(|| format!("{}: no permission prompt pending", w.label))
}

/// Which prompt a card or row showed: its `@agent_since` epoch and its
/// detail (as the view sanitized it). A new prompt, even for the same tool,
/// has a new `@agent_since`.
pub fn prompt_key(a: &Agent) -> String {
    format!("{} {}", a.since.unwrap_or(0), a.detail)
}

/// [`prompt_key`] of a snapshot row (the same sanitizing as the view's).
fn window_prompt(w: &Window) -> String {
    format!("{} {}", w.since_t.unwrap_or(0), crate::text::sanitize(&w.detail))
}

/// How long a guarded target (a chip, a diff badge) must have sat at a cell
/// before a click there is trusted ([`Arrivals`]).
pub const SETTLE: Duration = Duration::from_millis(400);

/// When each guarded click target first appeared at each cell, across the
/// frames drawn. Per target IDENTITY (an answer chip is its window, its
/// allow / deny and its prompt), not per frame: the spinner and the pulse
/// redraw every 125 ms, so "the frame is old enough" or "the frame before
/// had it" would trust a chip that slid under the pointer two redraws ago.
/// A click is trusted only where the same target has been for
/// [`SETTLE`]. Cards that come and go stack their chips in one column, so
/// this is what keeps a click aimed at a leaving card off the next one's.
#[derive(Debug, Clone)]
pub struct Arrivals<T> {
    since: std::collections::HashMap<(u16, u16), (T, std::time::Instant)>,
}

impl<T> Default for Arrivals<T> {
    fn default() -> Self {
        Arrivals { since: Default::default() }
    }
}

impl<T: Clone + PartialEq> Arrivals<T> {
    /// A frame reached the screen at `now`, with these guarded targets per
    /// cell: a cell keeps its time while the same target stays on it; a new
    /// one (or a cell no longer guarded) starts over.
    pub fn update(&mut self, cells: impl IntoIterator<Item = ((u16, u16), T)>, now: std::time::Instant) {
        let mut next = std::collections::HashMap::new();
        for (cell, t) in cells {
            let at = match self.since.get(&cell) {
                Some((old, at)) if *old == t => *at,
                _ => now,
            };
            next.insert(cell, (t, at));
        }
        self.since = next;
    }

    /// `t` has been at `cell` for [`SETTLE`] or more by `now`.
    pub fn settled(&self, cell: (u16, u16), t: &T, now: std::time::Instant) -> bool {
        self.since.get(&cell).is_some_and(|(old, at)| old == t && now.saturating_duration_since(*at) >= SETTLE)
    }
}

/// The pane values the check and the send must agree on ([`STATE_FMT`]).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PaneState {
    pub cursor_y: u32,
    pub history: u32,
    pub in_mode: bool,
    /// `#{pane_mode}`: "copy-mode", "view-mode", "tree-mode", … ("" none).
    pub mode: String,
    /// The window's `synchronize-panes` is on.
    pub sync: bool,
}

/// One `display-message -p` line of [`PaneState`], printed with the capture.
pub const STATE_FMT: &str = "#{cursor_y},#{history_size},#{pane_in_mode},#{pane_mode},#{synchronize-panes}";

impl PaneState {
    pub fn parse(line: &str) -> Option<PaneState> {
        let p: Vec<&str> = line.trim().split(',').collect();
        let [y, h, m, mode, sync] = p.as_slice() else { return None };
        Some(PaneState { cursor_y: y.parse().ok()?, history: h.parse().ok()?, in_mode: *m == "1",
            mode: mode.to_string(), sync: *sync == "1" })
    }
}

/// `s` as literal text inside a tmux format: `#` doubled, `,` and `}`
/// escaped (`#,` `#}`; verified on tmux 3.7 with `{x}`, `a,b`, `x}y`,
/// quotes, `$(…)`, backticks, backslashes). None for text a format cannot
/// carry on one line (a control character).
pub fn fmt_literal(s: &str) -> Option<String> {
    if s.chars().any(char::is_control) {
        return None;
    }
    Some(s.replace('#', "##").replace(',', "#,").replace('}', "#}"))
}

/// The prompt as the snapshot had it, for [`plan`]'s condition: the raw
/// `@agent_since` ("<epoch> needs-input") and `@agent_detail`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Asked<'a> {
    pub since: &'a str,
    pub detail: &'a str,
}

/// The ONE tmux command that answers, from the state read with the
/// capture: `if-shell -F -t <pane> <the same state still> '<keys> ;
/// display-message -p sent' 'display-message -p changed'`. "The same" is
/// the pane (cursor, history, mode, synchronized panes) AND the prompt
/// (`asked`: the window's `@agent_since` and `@agent_detail` as the
/// snapshot read them), so a re-ask between the snapshot and the send is
/// caught by tmux itself. Copy / view mode is cancelled in the same list;
/// another mode or synchronized panes are refused.
pub fn plan(st: &PaneState, d: Dialog, v: Verdict, pane: &str, asked: Asked) -> Result<Vec<String>, &'static str> {
    if st.sync {
        return Err("its window has synchronize-panes on");
    }
    if st.in_mode && !matches!(st.mode.as_str(), "copy-mode" | "view-mode") {
        return Err("its pane is in a tmux mode");
    }
    let (Some(since), Some(detail)) = (fmt_literal(asked.since), fmt_literal(asked.detail)) else {
        return Err("can't pin down its prompt");
    };
    let eq = |f: &str, v: String| format!("#{{==:#{{{f}}},{v}}}");
    let all = [
        eq("cursor_y", st.cursor_y.to_string()),
        eq("history_size", st.history.to_string()),
        eq("pane_in_mode", u8::from(st.in_mode).to_string()),
        eq("synchronize-panes", "0".into()),
        eq("@agent_since", since),
        eq("@agent_detail", detail),
    ];
    // `#{&&:a,b}` takes two: nest them right to left.
    let cond = all.into_iter().rev().reduce(|acc, c| format!("#{{&&:{c},{acc}}}")).expect("not empty");
    let mut then = String::new();
    if st.in_mode {
        then.push_str(&format!("send-keys -X -t {pane} cancel ; "));
    }
    then.push_str(&format!("send-keys -t {pane} {} ; display-message -p sent", keys(d, v).join(" ")));
    Ok(["if-shell", "-F", "-t", pane, &cond, &then, "display-message -p changed"].map(String::from).to_vec())
}

/// Answer the permission prompt of window `win` (as the CURRENT snapshot
/// has it): Ok with the message line's words, Err with the refusal.
/// `prompt`: the [`prompt_key`] the click or key saw (None: whatever is
/// pending, the command line's case); another prompt now is refused.
/// Nothing is sent unless [`detect`] confirmed the dialog on the agent's
/// pane, and the pane is still as it was then ([`plan`]).
pub fn answer(actions: &Actions, snap: &Snapshot, win: &str, v: Verdict, prompt: Option<&str>) -> Result<String, String> {
    let w = snap.rows(win).next().ok_or_else(|| PANE_GONE.to_string())?;
    if let Some(why) = refuse_window(w) {
        return Err(why);
    }
    if prompt.is_some_and(|p| p != window_prompt(w)) {
        return Err(format!("{}: that prompt changed · look again", w.label));
    }
    let pane = actions.agent_pane(&w.id, &w.kind)?;
    let tmux = &actions.tmux;
    let out = match tmux.run(&["display-message", "-p", "-t", &pane, STATE_FMT, ";", "capture-pane", "-p", "-t", &pane]) {
        Ok(o) if o.ok() => o.stdout,
        _ => return Err(PANE_GONE.into()),
    };
    let (first, screen) = out.split_once('\n').unwrap_or((&out, ""));
    let st = PaneState::parse(first).ok_or_else(|| format!("{}: can't read its pane", w.label))?;
    let d = detect(screen).map_err(|r| format!("{}: {}", w.label, r.words()))?;
    let asked = Asked { since: &w.since, detail: &w.detail };
    let cmd = plan(&st, d, v, &pane, asked).map_err(|e| format!("{}: {e}", w.label))?;
    match tmux.run(&cmd) {
        Ok(o) if o.ok() && o.stdout.trim() == "sent" => {
            discharge(actions, &w.id);
            Ok(format!("{} {}", v.done(), w.label))
        }
        Ok(o) if o.ok() => Err(format!("{}: its screen changed · try again", w.label)),
        _ => Err(format!("{}: couldn't send the keys", w.label)),
    }
}

/// agent-tab-indicator.sh, installed (`agent-tab-indicator.sh`) or in the
/// chezmoi source (`executable_…`).
fn indicator(actions: &Actions) -> std::path::PathBuf {
    let installed = actions.scripts.join("agent-tab-indicator.sh");
    let source = actions.scripts.join("executable_agent-tab-indicator.sh");
    if !installed.exists() && source.exists() { source } else { installed }
}

/// The prompt was answered: discharge the window as the focus hook would
/// (`agent-tab-indicator.sh clear-current <win>`: needs-input → idle,
/// `@agent_pending` stamped, so the allowed tool's heartbeat re-arms
/// `running`). `run-shell` (waited for) gives it the server's `$TMUX`,
/// which it needs; the sidebar itself runs outside tmux. Best effort.
fn discharge(actions: &Actions, win: &str) {
    let line = format!("bash {} clear-current {}", sh_quote(&indicator(actions).to_string_lossy()), sh_quote(win));
    let _ = actions.tmux.run(&["run-shell", &line]);
}

/// notify.sh with `text` as a warning (to `client` when given): `$NOTIFY_SH`,
/// else the scripts directory's. Pointed at this server like agent-jump.sh
/// ([`Tmux::script_env`]); `#` doubled, notify.sh expands formats.
fn notify(tmux: &Tmux, actions: &Actions, client: Option<&str>, text: &str) {
    let script = std::env::var("NOTIFY_SH").ok().filter(|s| !s.is_empty())
        .map_or_else(|| actions.scripts.join("notify.sh"), std::path::PathBuf::from);
    let mut c = Command::new(script);
    if let Some(cl) = client {
        c.args(["-c", cl]);
    }
    c.args(["-k", "warn", "--", &text.replace('#', "##")]);
    tmux.script_env(&mut c);
    let _ = proc::run(c, None, Duration::from_secs(5));
}

/// The subcommand.
pub fn run(tmux: Tmux, args: &[String]) -> ExitCode {
    let client = args.iter().position(|a| a == "--client").and_then(|i| args.get(i + 1)).cloned();
    let words: Vec<&String> = args.iter().enumerate()
        .filter(|(i, a)| *a != "--client" && (*i == 0 || args[i - 1] != "--client"))
        .map(|(_, a)| a)
        .collect();
    let (Some(win), Some(v), None) = (words.first(), words.get(1).and_then(|s| Verdict::parse(s)), words.get(2)) else {
        eprintln!("{USAGE}");
        return ExitCode::from(2);
    };
    let actions = Actions::new(tmux.clone());
    match answer(&actions, &tmux.snapshot(), win, v, None) {
        Ok(m) => {
            println!("{m}");
            ExitCode::SUCCESS
        }
        Err(m) => {
            eprintln!("agent-ui answer: {m}");
            notify(&tmux, &actions, client.as_deref(), &m);
            ExitCode::from(1)
        }
    }
}

#[cfg(test)]
mod tests;
