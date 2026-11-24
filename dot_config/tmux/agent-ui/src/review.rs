//! `agent-ui review`: prefix O, the "review old agents" popup. One stale
//! agent per screen, oldest first, space by space; a key closes, parks,
//! snoozes or keeps it, and the screen moves on (docs/review.md).
//!
//! ```text
//! agent-ui review --client <tty> [--min-idle SECS]
//! agent-ui review --count [--min-idle SECS]
//! agent-ui review --once WxH [--client <tty>] [--min-idle SECS] [--keys <script>] [--plain]
//! ```
//!
//! - **Candidates** ([`is_candidate`]): an agent window (`@agent_state`
//!   set) in a space (not `@space_hidden`, not a hidden or the stash
//!   session, not a space's home), its state idle, done, failed or
//!   needs-input (never running, paused or unknown; no workflow / cua work
//!   out), idle for at least [`MIN_IDLE`] by its `@agent_since` epoch, not
//!   pinned (`@agent_pinned`), not an abby-delegate child (its parent's card
//!   owns it), and not KEPT: `@review_kept` holds the `@agent_since` epoch
//!   it was kept at, so it comes back only once the agent has changed state
//!   since. Order ([`candidates`]): space by space in THE space order
//!   ([`crate::view::space_order`]'s key: ⌘N, unnumbered last by name), in a
//!   space the longest idle first.
//! - **Data**: ONE tmux call ([`query`]): the snapshot rows, plus each
//!   window's `@review_kept` (`K` rows) and each session's id (`S` rows, for
//!   space.sh close).
//! - **Actions** run as the menu's do, on the server (`run-shell -b`), each
//!   re-checked against a fresh snapshot first ([`recheck_in`]: gone, moved,
//!   working again, worked since the card, pinned now: refused, the reason
//!   over the key bar). A close / park / snooze is recorded only once its
//!   window has left its session ([`settle`], polled up to [`SETTLE`]);
//!   still there means its script refused (its toast says why): nothing is
//!   recorded and the card stays undecided.
//!   - `c`: `closed-tabs.sh close <agent pane>` (⌘Z reopens it; no
//!     `--ask-space`: this popup asks itself, below); a split tab is
//!     refused ([`close_refusal`]);
//!   - `p`: `stash.sh stash <win>` ([`Actions::park_line`], prefix H);
//!   - `s` + a preset or a typed time: `snooze.sh parse` here (a bad time
//!     says why under the box), then `snooze.sh apply -c <client> <epoch>
//!     w:@N` (park with an alarm);
//!   - `k` / Space: `set-option -w @review_kept <since epoch>`;
//!   - `v`: the popup closes, then [`Actions::go`] takes the client there.
//! - **Close the space**: when this review took the last agent out of a
//!   space that is not main nor hidden ([`space_emptied`]), the popup asks;
//!   y runs `review.sh close-space <sid> <client> <wins…>`, which waits for
//!   those windows to be gone and then runs `space.sh close` (prefix Q's).
//! - `--count`: just the number of candidates (one call, no TUI; the nudge
//!   timer's). `--once` renders one frame of the live server; `--keys`
//!   types a script into it first (`<esc>` `<enter>` `<left>` `<right>`
//!   `<bs>` `<up>` `<down>` `<tab>`, anything else a char) with every
//!   action DRY: what it would run is printed after the frame (`# …`).

pub mod view;

use self::view::{render, Cand, Outcome, Req, Review, SpaceInfo, LEFT, RIGHT};
use crate::actions::{park_refusal, park_refused_busy, sh_quote, Actions, PANE_GONE, TAB_MOVED};
use crate::menu::keys::{Key, KeyReader, ESC_WAIT};
use crate::model::{Harness, HIDDEN};
use crate::proc;
use crate::text;
use crate::tmux::{self, Snapshot, Tmux, Window, HOLD, US};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::Color;
use std::collections::HashMap;
use std::io::{self, BufWriter, Read, Write};
use std::process::{Command, ExitCode};
use std::sync::mpsc::{self, RecvTimeoutError, Sender};
use std::time::Duration;

const USAGE: &str = "usage: agent-ui review --client <tty> [--min-idle SECS]
       agent-ui review --count [--min-idle SECS]
       agent-ui review --once WxH [--client <tty>] [--min-idle SECS] [--keys <script>] [--plain]";

/// How long an agent must have sat idle to be up for review (8 hours).
pub const MIN_IDLE: i64 = 8 * 3600;
/// The window option `k` sets: the `@agent_since` epoch the agent was kept at.
pub const KEPT: &str = "@review_kept";
/// How many of the pane's last lines are read (the card drops the agent's
/// own furniture and shows what fits).
pub const PREVIEW_LINES: usize = 100;
const TICK: Duration = Duration::from_millis(250);
const POLL: Duration = Duration::from_millis(150);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Args {
    pub client: Option<String>,
    pub once: Option<(u16, u16)>,
    pub count: bool,
    pub min_idle: i64,
    pub keys: Option<String>,
    pub plain: bool,
}

impl Default for Args {
    fn default() -> Args {
        Args { client: None, once: None, count: false, min_idle: MIN_IDLE, keys: None, plain: false }
    }
}

pub fn parse_args(args: &[String]) -> Result<Args, String> {
    let mut a = Args::default();
    let mut it = args.iter();
    while let Some(arg) = it.next() {
        let mut val = || it.next().cloned().ok_or_else(|| format!("{arg} needs a value"));
        match arg.as_str() {
            "--client" => a.client = Some(val()?),
            "--count" => a.count = true,
            "--plain" => a.plain = true,
            "--keys" => a.keys = Some(val()?),
            "--min-idle" => {
                let v = val()?;
                a.min_idle = v.parse::<i64>().ok().filter(|n| *n >= 0).ok_or_else(|| format!("bad --min-idle {v:?}"))?;
            }
            "--once" => {
                let v = val()?;
                let (w, h) = v.split_once('x').ok_or_else(|| format!("--once wants WxH, not {v:?}"))?;
                let n = |s: &str| s.parse::<u16>().map_err(|_| format!("bad size {v:?}"));
                a.once = Some((n(w)?, n(h)?));
            }
            _ => return Err(format!("unknown argument {arg:?}")),
        }
    }
    Ok(a)
}

/// The one call: the snapshot's windows and clients, every window's
/// `@review_kept` (`K` rows), every session's id and name (`S` rows).
pub fn query() -> Vec<String> {
    let us = US.to_string();
    vec![
        "list-windows".into(), "-a".into(), "-F".into(), tmux::fmt(), ";".into(),
        "list-clients".into(), "-F".into(), tmux::client_fmt(), ";".into(),
        "list-windows".into(), "-a".into(), "-F".into(), ["K", "#{window_id}", &tmux::guarded("#{@review_kept}")].join(&us),
        ";".into(),
        "list-sessions".into(), "-F".into(), ["S", "#{session_id}", &tmux::guarded("#{session_name}")].join(&us),
    ]
}

/// What the popup reads, from [`query`]'s output.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct State {
    pub snap: Snapshot,
    /// Window id → `@review_kept` (set ones only).
    pub kept: HashMap<String, String>,
    /// Session name → `$N`.
    pub sids: HashMap<String, String>,
}

pub fn parse_state(text: &str) -> State {
    let mut st = State { snap: Snapshot::parse(text), ..State::default() };
    for line in text.lines() {
        let f: Vec<&str> = line.split(US).collect();
        match f.as_slice() {
            ["K", id, v] if !v.trim().is_empty() => {
                st.kept.insert(id.to_string(), tmux::restore(v).trim().to_string());
            }
            ["S", sid, name] => {
                st.sids.insert(tmux::restore(name), sid.to_string());
            }
            _ => {}
        }
    }
    st
}

/// A quiet state this review offers: idle, done, failed, needs-*.
fn quiet(state: &str) -> bool {
    matches!(state, "idle" | "done" | "failed") || state.starts_with("needs-")
}

/// The candidate rule (module docs), at `now` (epoch seconds).
pub fn is_candidate(w: &Window, kept: &HashMap<String, String>, now: i64, min_idle: i64) -> bool {
    let Some(since) = w.since_t else { return false };
    !w.state.is_empty()
        && quiet(&w.state)
        && !w.space_hidden
        && !HIDDEN.contains(&w.session.as_str())
        && w.session != HOLD
        && !w.is_pending()
        && !w.home
        && w.workflow.is_empty()
        && w.cua.is_empty()
        && w.pinned.is_none()
        && w.child.is_empty()
        && now - since >= min_idle
        && kept.get(&w.id).map(String::as_str) != Some(since.to_string().as_str())
}

/// THE space order's key for a window's session (⌘N first, the rest by
/// name; [`crate::view::space_order`]), then the longest idle first.
pub fn candidates<'a>(snap: &'a Snapshot, kept: &HashMap<String, String>, now: i64, min_idle: i64) -> Vec<&'a Window> {
    let mut seen = std::collections::HashSet::new();
    let mut v: Vec<&Window> =
        snap.windows.iter().filter(|w| is_candidate(w, kept, now, min_idle) && seen.insert(w.id.as_str())).collect();
    v.sort_by(|a, b| {
        (a.space_index.is_none(), a.space_index, &a.session, a.since_t, a.index)
            .cmp(&(b.space_index.is_none(), b.space_index, &b.session, b.since_t, b.index))
    });
    v
}

/// The space `session` when no agent is left in it once `removed` (window
/// ids) are gone, and it may be closed (not main, not hidden, still there,
/// its id known); None otherwise.
pub fn space_emptied(st: &State, session: &str, removed: &[String]) -> Option<SpaceInfo> {
    let rows: Vec<&Window> = st.snap.windows.iter().filter(|w| w.session == session).collect();
    let first = rows.first()?;
    if session == "main" || session == HOLD || first.space_hidden || HIDDEN.contains(&session) {
        return None;
    }
    if rows.iter().any(|w| !w.state.is_empty() && !removed.contains(&w.id)) {
        return None;
    }
    let shells = rows.iter().filter(|w| w.state.is_empty() && !removed.contains(&w.id)).count();
    let sid = st.sids.get(session)?.clone();
    Some(SpaceInfo { sid, name: session.into(), shells, wins: removed.to_vec() })
}

/// `#rrggbb` → its colour.
fn hex(s: &str) -> Option<Color> {
    let h = s.trim().strip_prefix('#').filter(|h| h.len() == 6 && h.is_ascii())?;
    let n = |i: usize| u8::from_str_radix(&h[i..i + 2], 16).ok();
    Some(Color::Rgb(n(0)?, n(2)?, n(4)?))
}

/// A window → its card.
pub fn cand_of(w: &Window) -> Cand {
    let reply = if matches!(w.detail_kind.as_str(), "done" | "ask" | "fail") { text::plain(&w.detail) } else { String::new() };
    Cand {
        id: w.id.clone(),
        session: w.session.clone(),
        space_index: w.space_index,
        space_color: hex(&w.space_color),
        key: w.key.trim().to_string(),
        title: if w.summary.trim().is_empty() { w.name.clone() } else { w.summary.clone() },
        harness: Harness::of(&w.harness, &w.kind),
        state: w.state.clone(),
        since: w.since_t.unwrap_or(0),
        reply,
        preview: None,
    }
}

/// `--keys`' script → keys.
pub fn parse_keyspec(s: &str) -> Vec<Key> {
    let mut out = Vec::new();
    let mut rest = s;
    while let Some(c) = rest.chars().next() {
        if c == '<' {
            if let Some(end) = rest.find('>') {
                let k = match &rest[1..end] {
                    "esc" => Some(Key::Esc),
                    "enter" => Some(Key::Enter),
                    "left" => Some(Key::Char(LEFT)),
                    "right" => Some(Key::Char(RIGHT)),
                    "bs" => Some(Key::Backspace),
                    "up" => Some(Key::Up),
                    "down" => Some(Key::Down),
                    "tab" => Some(Key::Tab),
                    _ => None,
                };
                if let Some(k) = k {
                    out.push(k);
                    rest = &rest[end + 1..];
                    continue;
                }
            }
        }
        out.push(Key::Char(c));
        rest = &rest[c.len_utf8()..];
    }
    out
}

/// ← / → (CSI or SS3 D / C) → [`LEFT`] / [`RIGHT`] as UTF-8, before the
/// shared key reader (which swallows them) sees the bytes. A sequence torn
/// across two reads is left alone (and swallowed: nothing happens).
pub fn arrows(bytes: &[u8]) -> Vec<u8> {
    let (l, r) = (LEFT.to_string(), RIGHT.to_string());
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        let s = &bytes[i..];
        if s.len() >= 3 && s[0] == 0x1b && (s[1] == b'[' || s[1] == b'O') && (s[2] == b'D' || s[2] == b'C') {
            out.extend_from_slice(if s[2] == b'D' { l.as_bytes() } else { r.as_bytes() });
            i += 3;
        } else {
            out.push(s[0]);
            i += 1;
        }
    }
    out
}

/// What the loop does after a request.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Flow {
    Stay,
    Quit,
    Jump(usize),
}

/// Carries out [`Req`]s. `dry`: nothing runs, every command is logged.
struct Runner {
    tmux: Tmux,
    actions: Actions,
    client: String,
    dry: bool,
    st: State,
    /// The candidates' windows, in the cards' order.
    wins: Vec<Window>,
    log: Vec<String>,
    /// A close / park / snooze handed to the server, not yet seen to land.
    pending: Option<Pending>,
}

/// How long a close / park / snooze has to take its window out of its
/// session before the review calls it refused (stash.sh refuses with a
/// toast: unsent text in the composer, no session id, busy, …).
pub const SETTLE: Duration = Duration::from_secs(3);

/// An action out on candidate `i`, recorded as `outcome` / `msg` once its
/// window has left; `ing` is the message meanwhile, `who` the script
/// named when it did not.
#[derive(Debug, Clone)]
struct Pending {
    i: usize,
    outcome: Outcome,
    msg: String,
    ing: &'static str,
    who: &'static str,
    until: std::time::Instant,
}

impl Pending {
    fn new(i: usize, outcome: Outcome, msg: String, ing: &'static str, who: &'static str) -> Pending {
        Pending { i, outcome, msg, ing, who, until: std::time::Instant::now() + SETTLE }
    }
}

/// A pending action's state, from whether its window has left its session
/// and whether [`SETTLE`] is over.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Settle {
    Left,
    Waiting,
    Refused,
}

pub fn settle(left: bool, timed_out: bool) -> Settle {
    match (left, timed_out) {
        (true, _) => Settle::Left,
        (false, false) => Settle::Waiting,
        (false, true) => Settle::Refused,
    }
}

/// The window is no longer in `session` (closed, or moved into the stash).
pub fn left_session(st: &State, id: &str, session: &str) -> bool {
    st.snap.row_in(id, session).is_none()
}

/// The card's window `c` (as the review listed it) as `st` has it now, or
/// the message's reason: gone; moved to another session; working again;
/// worked since the card was made (its `@agent_since` changed); pinned now.
pub fn recheck_in<'a>(st: &'a State, c: &Window) -> Result<&'a Window, String> {
    if st.snap.rows(&c.id).next().is_none() {
        return Err(PANE_GONE.into());
    }
    let w = st.snap.row_in(&c.id, &c.session).ok_or_else(|| format!("{TAB_MOVED} · left alone"))?;
    let l = text::sanitize(&w.label);
    if park_refused_busy(w) {
        return Err(format!("{l} is working again · left alone"));
    }
    if w.since_t != c.since_t {
        return Err(format!("{l} worked since this card · left alone"));
    }
    if w.pinned.is_some() {
        return Err(format!("{l} is pinned now · left alone"));
    }
    Ok(w)
}

/// Closing a SPLIT tab would take only the agent's pane: the window stays
/// (and its space keeps it), so the review refuses it, as park does.
pub const SPLIT_CLOSE: &str = "split tab · close the agent from its tab";

pub fn close_refusal(w: &Window) -> Result<(), String> {
    if w.panes > 1 {
        return Err(SPLIT_CLOSE.into());
    }
    Ok(())
}

impl Runner {
    fn script(&self, name: &str) -> String {
        sh_quote(&self.actions.scripts.join(name).to_string_lossy())
    }

    fn bg(&mut self, line: String) {
        if self.dry {
            self.log.push(format!("run-shell -b {line}"));
        } else {
            self.tmux.run_bg(&line);
        }
    }

    fn refresh(&mut self) {
        if self.dry {
            return;
        }
        if let Ok(o) = self.tmux.run(&query()) {
            if o.ok() {
                self.st = parse_state(&o.stdout);
            }
        }
    }

    /// Candidate `i`'s window as it is now ([`recheck_in`] on a fresh snapshot).
    fn recheck(&mut self, i: usize) -> Result<Window, String> {
        self.refresh();
        recheck_in(&self.st, &self.wins[i]).cloned()
    }

    /// An action is out on candidate `i`: wait (polled by [`poll`](Self::poll))
    /// for its window to leave its session before anything is recorded.
    fn start(&mut self, r: &mut Review, p: Pending) {
        r.mode = view::Mode::Agent;
        if self.dry {
            r.record(p.i, p.outcome, p.msg);
            self.after_remove(r, p.i);
            return;
        }
        r.busy = true;
        r.flash = Some((format!("{}…", p.ing), false));
        self.pending = Some(p);
    }

    /// The pending action, checked against a fresh snapshot: the window left
    /// its session → recorded (and the space question); still there past
    /// [`SETTLE`] → refused, nothing recorded, the card stays undecided.
    fn poll(&mut self, r: &mut Review) {
        let Some(p) = self.pending.as_ref() else { return };
        let (id, session) = (self.wins[p.i].id.clone(), self.wins[p.i].session.clone());
        let timed_out = std::time::Instant::now() >= p.until;
        self.refresh();
        match settle(left_session(&self.st, &id, &session), timed_out) {
            Settle::Waiting => {}
            Settle::Left => {
                let Some(p) = self.pending.take() else { return };
                r.busy = false;
                r.record(p.i, p.outcome, p.msg);
                self.after_remove(r, p.i);
            }
            Settle::Refused => {
                let Some(p) = self.pending.take() else { return };
                r.busy = false;
                r.refused(format!("{} refused (see toast) · left alone", p.who));
            }
        }
    }

    fn fill_preview(&self, r: &mut Review) {
        let i = r.cur;
        let Some(c) = r.cands.get_mut(i) else { return };
        if c.preview.is_some() {
            return;
        }
        let lines = self.actions.peek(&self.wins[i], PREVIEW_LINES).unwrap_or_default();
        let lines: Vec<String> = lines.iter().map(|l| l.plain()).skip_while(|l| l.trim().is_empty()).collect();
        c.preview = Some(lines);
    }

    /// After an agent left its space: ask about the space, or move on.
    fn after_remove(&mut self, r: &mut Review, i: usize) {
        self.refresh();
        let session = self.wins[i].session.clone();
        match space_emptied(&self.st, &session, &r.removed_in(&session)) {
            Some(info) => r.prompt_space(info),
            None => r.advance(),
        }
    }

    fn snooze_epoch(&self, when: &str) -> Result<i64, String> {
        let mut c = Command::new("bash");
        c.arg(self.actions.scripts.join("snooze.sh")).arg("parse").arg(when);
        match proc::run(c, None, Duration::from_secs(5)) {
            Ok(o) if o.ok() => o.stdout.trim().parse::<i64>().map_err(|_| format!("snooze: can't read '{when}'")),
            Ok(o) => Err(o.stderr.lines().next().map(str::to_string).unwrap_or_else(|| format!("snooze: can't read '{when}'"))),
            Err(_) => Err("snooze.sh parse failed".into()),
        }
    }

    fn apply(&mut self, r: &mut Review, req: Req) -> Flow {
        match req {
            Req::Quit => return Flow::Quit,
            Req::Jump(i) => {
                // Gone or moved (closed, parked): say so here, the popup stays.
                self.refresh();
                let c = &self.wins[i];
                if self.st.snap.row_in(&c.id, &c.session).is_none() {
                    let why = if self.st.snap.rows(&c.id).next().is_none() { PANE_GONE } else { TAB_MOVED };
                    r.refused(format!("{why} · nothing to go to"));
                    return Flow::Stay;
                }
                return Flow::Jump(i);
            }
            Req::Close(i) => match self.close(i) {
                Ok(m) => self.start(r, Pending::new(i, Outcome::Closed, m, "closing", "closed-tabs.sh")),
                Err(e) => r.refused(e),
            },
            Req::Park(i) => match self.recheck(i).and_then(|w| park_refusal(&w, "park").map(|_| w)) {
                Ok(w) => {
                    let line = self.actions.park_line(&w.id);
                    self.bg(line);
                    let m = format!("parked {} · prefix h brings it back", text::sanitize(&r.cands[i].title));
                    self.start(r, Pending::new(i, Outcome::Parked, m, "parking", "stash.sh"));
                }
                Err(e) => r.refused(e),
            },
            Req::Snooze(i, when) => {
                let res = self.snooze_epoch(&when).and_then(|t| {
                    let w = self.recheck(i)?;
                    park_refusal(&w, "snooze")?;
                    Ok((t, w))
                });
                match res {
                    Ok((t, w)) => {
                        let line = format!("{} apply -c {} {t} {}", self.script("snooze.sh"), sh_quote(&self.client),
                            sh_quote(&format!("w:{}", w.id)));
                        self.bg(line);
                        let back = text::wake_phrase(&text::wake(t, text::now()));
                        let m = format!("snoozed {} · back {back}", text::sanitize(&r.cands[i].title));
                        self.start(r, Pending::new(i, Outcome::Snoozed, m, "snoozing", "stash.sh"));
                    }
                    Err(e) => r.refused(e),
                }
            }
            Req::Keep(i) => {
                // Only the since the card showed: an agent that worked since
                // is refused like any action (recheck).
                let w = match self.recheck(i) {
                    Ok(w) => w,
                    Err(e) => {
                        r.refused(e);
                        return Flow::Stay;
                    }
                };
                let (id, since) = (w.id.clone(), w.since_t.unwrap_or(r.cands[i].since).to_string());
                let args = ["set-option", "-w", "-t", id.as_str(), KEPT, since.as_str()];
                let ok = if self.dry {
                    self.log.push(format!("tmux {}", args.join(" ")));
                    true
                } else {
                    self.tmux.run(&args).is_ok_and(|o| o.ok())
                };
                if ok {
                    r.record(i, Outcome::Kept, format!("kept {} · asked again once it has worked", text::sanitize(&r.cands[i].title)));
                    r.advance();
                } else {
                    r.refused(PANE_GONE.into());
                }
            }
            Req::CloseSpace(info) => {
                let mut line = format!("{} close-space {} {}", self.script("review.sh"), sh_quote(&info.sid), sh_quote(&self.client));
                for w in &info.wins {
                    line.push(' ');
                    line.push_str(&sh_quote(w));
                }
                self.bg(line);
                r.spaces_closed += 1;
                r.advance();
                r.flash = Some((format!("closing space {} · ⌘Z reopens its tabs", text::sanitize(&info.name)), false));
            }
        }
        Flow::Stay
    }

    /// `c`: the agent's pane through closed-tabs.sh (no `--ask-space`).
    fn close(&mut self, i: usize) -> Result<String, String> {
        let w = self.recheck(i)?;
        close_refusal(&w)?;
        let pane = if self.dry { w.id.clone() } else { self.actions.agent_pane(&w.id, &w.kind)? };
        let line = format!("{} close {}", self.script("closed-tabs.sh"), sh_quote(&pane));
        self.bg(line);
        Ok(format!("closed {} · ⌘Z brings it back", text::sanitize(&w.label)))
    }
}

pub fn run(tmux: Tmux, args: &[String]) -> ExitCode {
    let a = match parse_args(args) {
        Ok(a) => a,
        Err(e) => {
            eprintln!("agent-ui review: {e}\n{USAGE}");
            return ExitCode::from(2);
        }
    };
    let st = match tmux.run(&query()) {
        Ok(o) if o.ok() => parse_state(&o.stdout),
        _ if a.count => {
            eprintln!("agent-ui review: no tmux server");
            return ExitCode::from(1);
        }
        _ => State::default(),
    };
    let now = text::now().floor() as i64;
    let picked: Vec<Window> = candidates(&st.snap, &st.kept, now, a.min_idle).into_iter().cloned().collect();
    if a.count {
        println!("{}", picked.len());
        return ExitCode::SUCCESS;
    }
    let hour = text::local(now).map_or(12, |l| l.hour);
    let mut r = Review::new(picked.iter().map(cand_of).collect(), now, hour, a.min_idle);
    let mut runner = Runner {
        actions: Actions::new(tmux.clone()),
        tmux,
        client: a.client.clone().unwrap_or_default(),
        dry: a.once.is_some(),
        st,
        wins: picked,
        log: Vec::new(),
        pending: None,
    };
    if let Some((w, h)) = a.once {
        for k in parse_keyspec(a.keys.as_deref().unwrap_or("")) {
            if let Some(req) = r.key(k) {
                if runner.apply(&mut r, req.clone()) != Flow::Stay {
                    runner.log.push(format!("{req:?}"));
                    break;
                }
            }
        }
        runner.fill_preview(&mut r);
        let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
        render(&r, &mut buf);
        let lines = crate::menu::render::dump(&buf, !a.plain);
        let lines = if a.plain { crate::launch::view::initials(&lines) } else { lines };
        let mut out = io::stdout().lock();
        for line in lines.iter().chain(runner.log.iter().map(|l| format!("# {l}")).collect::<Vec<_>>().iter()) {
            let _ = writeln!(out, "{line}");
        }
        return ExitCode::SUCCESS;
    }
    match interactive(&mut r, &mut runner) {
        Ok(Flow::Jump(i)) => {
            let w = runner.wins[i].clone();
            runner.refresh();
            if let Err(e) = runner.actions.go(&runner.st.snap, &runner.client, &w.id, &w.session) {
                eprintln!("agent-ui review: {e}");
            }
            ExitCode::SUCCESS
        }
        Ok(_) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("agent-ui review: {e}");
            ExitCode::from(1)
        }
    }
}

/// Raw mode and the alternate screen, undone on drop.
struct TermGuard;

impl TermGuard {
    fn new() -> io::Result<TermGuard> {
        crossterm::terminal::enable_raw_mode()?;
        let mut o = io::stdout().lock();
        o.write_all(b"\x1b[?1049h\x1b[?25l\x1b[2J")?;
        o.flush()?;
        Ok(TermGuard)
    }
}

impl Drop for TermGuard {
    fn drop(&mut self) {
        let mut o = io::stdout().lock();
        let _ = o.write_all(b"\x1b[0m\x1b[?25h\x1b[?1049l");
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
                    if tx.send(Msg::Input(arrows(&buf[..n]))).is_err() {
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

fn draw(term: &mut Term, r: &Review, last: &mut Option<Buffer>) -> io::Result<()> {
    let (w, h) = crossterm::terminal::size()?;
    let mut buf = Buffer::empty(Rect::new(0, 0, w, h));
    render(r, &mut buf);
    if last.as_ref() == Some(&buf) {
        return Ok(());
    }
    term.draw(|f| {
        let b = f.buffer_mut();
        if b.area == buf.area {
            b.content.clone_from(&buf.content);
        } else {
            render(r, b);
        }
    })?;
    *last = Some(buf);
    Ok(())
}

/// Until a key quits or jumps (stdin closed: quit).
fn interactive(r: &mut Review, runner: &mut Runner) -> io::Result<Flow> {
    let _guard = TermGuard::new()?;
    let (tx, rx) = mpsc::channel::<Msg>();
    spawn_reader(tx);
    let mut term: Term =
        ratatui::Terminal::new(ratatui::backend::CrosstermBackend::new(BufWriter::with_capacity(1 << 16, io::stdout())))?;
    let mut reader = KeyReader::new();
    let mut last: Option<Buffer> = None;
    loop {
        runner.poll(r);
        if r.mode == view::Mode::Agent && !r.busy {
            runner.fill_preview(r);
        }
        draw(&mut term, r, &mut last)?;
        // A pending action is polled more often than the idle tick.
        let tick = if runner.pending.is_some() { POLL } else { TICK };
        let bytes = match rx.recv_timeout(tick) {
            Ok(Msg::Input(b)) => b,
            Ok(Msg::Eof) | Err(RecvTimeoutError::Disconnected) => return Ok(Flow::Quit),
            Err(RecvTimeoutError::Timeout) => continue,
        };
        let mut keys = reader.feed(&bytes);
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
        if let Some(req) = r.handle(&keys) {
            match runner.apply(r, req) {
                Flow::Stay => {}
                f => return Ok(f),
            }
        }
        if eof {
            return Ok(Flow::Quit);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tmux::tests::row;

    const NOW: i64 = 1_800_000_000;
    const H: i64 = 3600;

    fn since(age: i64, state: &str) -> String {
        format!("{} {state}", NOW - age)
    }

    fn agent(sess: &str, idx: u32, id: &str, age: i64, state: &str, extra: &[(&str, &str)]) -> String {
        let s = since(age, state);
        let mut set: Vec<(&str, &str)> = vec![("state", state), ("since", &s), ("summary", "T")];
        set.extend_from_slice(extra);
        row(sess, idx, id, &set)
    }

    fn snap(rows: &[String]) -> Snapshot {
        Snapshot::parse(&rows.join("\n"))
    }

    fn ids(v: &[&Window]) -> Vec<String> {
        v.iter().map(|w| w.id.clone()).collect()
    }

    #[test]
    fn args() {
        let s = |v: &[&str]| v.iter().map(|x| x.to_string()).collect::<Vec<_>>();
        let a = parse_args(&s(&["--client", "/dev/ttys004"])).unwrap();
        assert_eq!((a.client.as_deref(), a.min_idle, a.count), (Some("/dev/ttys004"), MIN_IDLE, false));
        let a = parse_args(&s(&["--count", "--min-idle", "60"])).unwrap();
        assert_eq!((a.count, a.min_idle), (true, 60));
        let a = parse_args(&s(&["--once", "70x20", "--keys", "s1", "--plain"])).unwrap();
        assert_eq!((a.once, a.keys.as_deref(), a.plain), (Some((70, 20)), Some("s1"), true));
        assert!(parse_args(&s(&["--min-idle", "-3"])).is_err());
        assert!(parse_args(&s(&["--min-idle"])).is_err());
        assert!(parse_args(&s(&["--bogus"])).is_err());
    }

    /// The candidate rule: quiet states, the threshold, pinned, kept,
    /// running / paused / busy / home / hidden / stash / child excluded.
    #[test]
    fn the_candidate_filter() {
        let ix = [("space_index", "2")];
        let s = snap(&[
            agent("notes", 1, "@1", 9 * H, "idle", &ix),
            agent("notes", 2, "@2", 9 * H, "done", &ix),
            agent("notes", 3, "@3", 9 * H, "failed", &ix),
            agent("notes", 4, "@4", 9 * H, "needs-input", &ix),
            agent("notes", 5, "@5", 9 * H, "running", &ix),
            agent("notes", 6, "@6", 9 * H, "done", &[("space_index", "2"), ("paused", "1799990000")]), // paused
            agent("notes", 7, "@7", 8 * H - 1, "idle", &ix), // just under
            agent("notes", 8, "@8", 8 * H, "idle", &ix),     // exactly at
            agent("notes", 9, "@9", 9 * H, "idle", &[("space_index", "2"), ("pinned", "1799000000")]),
            agent("notes", 10, "@10", 9 * H, "idle", &ix), // kept at its since
            agent("notes", 11, "@11", 9 * H, "idle", &ix), // kept at an OLDER since: back
            agent("notes", 12, "@12", 9 * H, "idle", &[("space_index", "2"), ("workflow", "1")]),
            agent("notes", 13, "@13", 9 * H, "idle", &[("space_index", "2"), ("home", "1")]),
            agent("agents", 1, "@14", 9 * H, "idle", &[("space_hidden", "1")]),
            agent("stash", 1, "@15", 9 * H, "idle", &[("space_hidden", "1")]),
            agent("notes", 14, "@16", 9 * H, "idle", &[("space_index", "2"), ("child", "@1")]),
            row("notes", 15, "@17", &[("space_index", "2")]), // a shell
            agent("notes", 16, "@18", 9 * H, "weird", &ix),
        ]);
        let mut kept = HashMap::new();
        kept.insert("@10".to_string(), (NOW - 9 * H).to_string());
        kept.insert("@11".to_string(), (NOW - 20 * H).to_string());
        let got = candidates(&s, &kept, NOW, MIN_IDLE);
        assert_eq!(ids(&got), ["@1", "@2", "@3", "@4", "@11", "@8"]);
        // The threshold is a parameter.
        assert_eq!(candidates(&s, &kept, NOW, 10 * H).len(), 0);
        assert!(ids(&candidates(&s, &kept, NOW, 0)).contains(&"@7".to_string()));
    }

    /// Space by space in ⌘N order (unnumbered last, by name), longest idle
    /// first within a space; a linked window once.
    #[test]
    fn the_order() {
        let s = snap(&[
            agent("zeta", 1, "@1", 10 * H, "idle", &[]),
            agent("alpha", 1, "@2", 10 * H, "idle", &[]),
            agent("work", 2, "@3", 9 * H, "idle", &[("space_index", "3")]),
            agent("work", 3, "@4", 30 * H, "done", &[("space_index", "3")]),
            agent("main", 4, "@5", 9 * H, "idle", &[("space_index", "1")]),
            agent("notes", 1, "@6", 12 * H, "idle", &[("space_index", "2")]),
            agent("notes", 2, "@7", 50 * H, "idle", &[("space_index", "2")]),
            agent("work", 4, "@7", 50 * H, "idle", &[("space_index", "3")]), // @7 linked
        ]);
        assert_eq!(ids(&candidates(&s, &HashMap::new(), NOW, MIN_IDLE)), ["@5", "@7", "@6", "@4", "@3", "@2", "@1"]);
    }

    fn state(rows: &[String], sids: &[(&str, &str)]) -> State {
        State { snap: snap(rows), sids: sids.iter().map(|(n, s)| (n.to_string(), s.to_string())).collect(), ..State::default() }
    }

    /// The last agent of a space: once every agent there is among the
    /// removed; never main, hidden or the stash; shells counted.
    #[test]
    fn last_agent_of_a_space() {
        let rows = [
            row("notes", 1, "@1", &[("space_index", "2"), ("home", "1")]),
            agent("notes", 2, "@2", 9 * H, "idle", &[("space_index", "2")]),
            agent("notes", 3, "@3", 9 * H, "running", &[("space_index", "2")]),
            row("notes", 4, "@4", &[("space_index", "2")]),
            agent("main", 2, "@5", 9 * H, "idle", &[("space_index", "1")]),
            agent("agents", 1, "@6", 9 * H, "idle", &[("space_hidden", "1")]),
            agent("solo", 1, "@7", 9 * H, "idle", &[("space_index", "3")]),
        ];
        let st = state(&rows, &[("notes", "$2"), ("main", "$0"), ("agents", "$9"), ("solo", "$3")]);
        let rm = |v: &[&str]| v.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        // A running agent is still there.
        assert_eq!(space_emptied(&st, "notes", &rm(&["@2"])), None);
        assert_eq!(space_emptied(&st, "notes", &rm(&["@2", "@3"])),
            Some(SpaceInfo { sid: "$2".into(), name: "notes".into(), shells: 2, wins: rm(&["@2", "@3"]) }));
        // A space of one agent, no home: zero shells.
        assert_eq!(space_emptied(&st, "solo", &rm(&["@7"])).map(|i| i.shells), Some(0));
        assert_eq!(space_emptied(&st, "main", &rm(&["@5"])), None);
        assert_eq!(space_emptied(&st, "agents", &rm(&["@6"])), None);
        assert_eq!(space_emptied(&st, "gone", &rm(&["@6"])), None);
        // No id for it: no prompt (space.sh close needs one).
        let st2 = state(&rows, &[]);
        assert_eq!(space_emptied(&st2, "solo", &rm(&["@7"])), None);
    }

    #[test]
    fn state_from_one_call() {
        let text = [
            agent("notes", 2, "@2", 9 * H, "idle", &[("space_index", "2")]),
            "K\x1f@2\x1f1799967600".to_string(),
            "K\x1f@3\x1f".to_string(),
            "S\x1f$2\x1fnotes".to_string(),
            "S\x1f$5\x1fa\u{E01F}b".to_string(),
        ]
        .join("\n");
        let st = parse_state(&text);
        assert_eq!(st.snap.windows.len(), 1);
        assert_eq!(st.kept.get("@2").map(String::as_str), Some("1799967600"));
        assert!(!st.kept.contains_key("@3"));
        assert_eq!(st.sids.get("notes").map(String::as_str), Some("$2"));
        assert_eq!(st.sids.get("a\x1fb").map(String::as_str), Some("$5"));
        let q = query();
        assert_eq!(q.iter().filter(|a| *a == ";").count(), 3);
        assert!(q.iter().any(|a| a.contains("@review_kept")));
    }

    #[test]
    fn cards_and_keys() {
        let s = snap(&[agent("notes", 2, "@2", 9 * H, "done",
            &[("space_index", "2"), ("key", ";a"), ("detail_kind", "done"), ("detail", "**All** done."), ("kind", "codex"),
                ("space_color", "#94e2d5")])]);
        let c = cand_of(&s.windows[0]);
        assert_eq!((c.key.as_str(), c.title.as_str(), c.reply.as_str(), c.harness.name()), (";a", "T", "All done.", "codex"));
        assert_eq!((c.since, c.space_color), (NOW - 9 * H, Some(Color::Rgb(0x94, 0xe2, 0xd5))));
        assert_eq!(parse_keyspec("s<down><enter>c<left><x>"),
            [Key::Char('s'), Key::Down, Key::Enter, Key::Char('c'), Key::Char(LEFT), Key::Char('<'), Key::Char('x'), Key::Char('>')]);
        assert_eq!(arrows(b"a\x1b[Db\x1bOC\x1b[A"), format!("a{LEFT}b{RIGHT}\x1b[A").into_bytes());
    }

    /// An action re-checks the card's window: gone, moved, working, worked
    /// since the card (its since changed), pinned now → refused.
    #[test]
    fn recheck_since_pinned_busy() {
        let card = Snapshot::parse(&agent("notes", 2, "@2", 9 * H, "idle", &[])).windows.remove(0);
        let st = |rows: &[String]| state(rows, &[]);
        assert!(recheck_in(&st(&[agent("notes", 2, "@2", 9 * H, "idle", &[])]), &card).is_ok());
        assert_eq!(recheck_in(&st(&[]), &card).unwrap_err(), PANE_GONE);
        assert!(recheck_in(&st(&[agent("stash", 2, "@2", 9 * H, "idle", &[])]), &card).unwrap_err().starts_with(TAB_MOVED));
        assert_eq!(recheck_in(&st(&[agent("notes", 2, "@2", 9 * H, "running", &[])]), &card).unwrap_err(),
            "T is working again · left alone");
        // It ran and went idle again during the review: a new since.
        assert_eq!(recheck_in(&st(&[agent("notes", 2, "@2", 60, "idle", &[])]), &card).unwrap_err(),
            "T worked since this card · left alone");
        assert_eq!(recheck_in(&st(&[agent("notes", 2, "@2", 9 * H, "idle", &[("pinned", "1799999000")])]), &card).unwrap_err(),
            "T is pinned now · left alone");
    }

    /// Close refuses a split tab (only the agent's pane would go).
    #[test]
    fn close_refuses_a_split() {
        let one = Snapshot::parse(&agent("notes", 2, "@2", 9 * H, "idle", &[("pane_flags", "A0%5:1 ")])).windows.remove(0);
        let two = Snapshot::parse(&agent("notes", 2, "@2", 9 * H, "idle", &[("pane_flags", "A0%5:1 p1%6:2 ")])).windows.remove(0);
        // A home's dashboard pane does not count.
        let dash = Snapshot::parse(&agent("notes", 2, "@2", 9 * H, "idle", &[("pane_flags", "d0%4:1 A1%5:2 ")])).windows.remove(0);
        assert_eq!((close_refusal(&one), close_refusal(&dash)), (Ok(()), Ok(())));
        assert_eq!(close_refusal(&two), Err(SPLIT_CLOSE.to_string()));
    }

    /// A pending action lands only once its window has left its session;
    /// still there past SETTLE is a refusal.
    #[test]
    fn confirm_removal() {
        let st = state(&[agent("notes", 2, "@2", 9 * H, "idle", &[]), agent("stash", 1, "@3", 9 * H, "idle", &[])], &[]);
        assert!(!left_session(&st, "@2", "notes"));
        assert!(left_session(&st, "@3", "notes")); // parked: in the stash now
        assert!(left_session(&st, "@9", "notes")); // closed: gone
        assert_eq!(settle(true, false), Settle::Left);
        assert_eq!(settle(true, true), Settle::Left);
        assert_eq!(settle(false, false), Settle::Waiting);
        assert_eq!(settle(false, true), Settle::Refused);
    }

    fn dry(st: State) -> (Review, Runner) {
        let wins: Vec<Window> = candidates(&st.snap, &st.kept, NOW, MIN_IDLE).into_iter().cloned().collect();
        let r = Review::new(wins.iter().map(cand_of).collect(), NOW, 10, MIN_IDLE);
        let run = Runner {
            tmux: Tmux::with_socket(None),
            actions: Actions { tmux: Tmux::with_socket(None), scripts: "/s".into() },
            client: "/dev/ttys004".into(),
            dry: true,
            st,
            wins,
            log: vec![],
            pending: None,
        };
        (r, run)
    }

    /// `v` on a card whose window is gone or moved: the reason in the
    /// message, the popup stays; a window still there jumps. Keep on an
    /// agent that worked since: refused, nothing written.
    #[test]
    fn jump_and_keep_recheck() {
        let rows = [agent("notes", 2, "@2", 20 * H, "idle", &[]), agent("notes", 3, "@3", 9 * H, "idle", &[])];
        let (mut r, mut run) = dry(state(&rows, &[("notes", "$2")]));
        assert_eq!(run.apply(&mut r, Req::Jump(0)), Flow::Jump(0));
        run.st = state(&[agent("stash", 2, "@2", 20 * H, "idle", &[]), agent("notes", 3, "@3", 60, "idle", &[])], &[]);
        assert_eq!(run.apply(&mut r, Req::Jump(0)), Flow::Stay);
        assert_eq!(r.flash.as_ref().map(|f| f.0.as_str()), Some("that tab moved · nothing to go to"));
        run.st = state(&[], &[]);
        assert_eq!(run.apply(&mut r, Req::Jump(0)), Flow::Stay);
        assert_eq!(r.flash.as_ref().map(|f| f.0.as_str()), Some("that window is gone · nothing to go to"));
        run.st = state(&[agent("notes", 3, "@3", 60, "idle", &[])], &[]);
        r.cur = 1;
        assert_eq!(run.apply(&mut r, Req::Keep(1)), Flow::Stay);
        assert_eq!((r.outcomes[1], r.cur), (None, 1));
        assert!(run.log.is_empty());
    }

    /// A dry run through the runner: each action logs what it would run and
    /// the screen moves on; emptying a space asks.
    #[test]
    fn dry_runner() {
        let rows = [
            agent("notes", 2, "@2", 20 * H, "idle", &[("space_index", "2")]),
            agent("notes", 3, "@3", 9 * H, "done", &[("space_index", "2")]),
            agent("work", 2, "@4", 9 * H, "idle", &[("space_index", "3")]),
        ];
        let st = state(&rows, &[("notes", "$2"), ("work", "$3")]);
        let wins: Vec<Window> = candidates(&st.snap, &st.kept, NOW, MIN_IDLE).into_iter().cloned().collect();
        let mut r = Review::new(wins.iter().map(cand_of).collect(), NOW, 10, MIN_IDLE);
        let mut run = Runner {
            tmux: Tmux::with_socket(None),
            actions: Actions { tmux: Tmux::with_socket(None), scripts: "/s".into() },
            client: "/dev/ttys004".into(),
            dry: true,
            st,
            wins,
            log: vec![],
            pending: None,
        };
        let mut go = |r: &mut Review, k: &str| {
            for key in parse_keyspec(k) {
                if let Some(req) = r.key(key) {
                    assert_eq!(run.apply(r, req), Flow::Stay);
                }
            }
        };
        go(&mut r, "k");
        assert_eq!((r.cur, r.outcomes[0]), (1, Some(Outcome::Kept)));
        go(&mut r, "c");
        // Kept is not removed: notes still has @2.
        assert_eq!((r.cur, &r.mode), (2, &view::Mode::Agent));
        go(&mut r, "p");
        // work's only agent parked: the prompt; y closes it.
        assert!(matches!(&r.mode, view::Mode::Space(i) if i.sid == "$3" && i.wins == ["@4"]));
        go(&mut r, "y");
        assert_eq!(r.mode, view::Mode::Done);
        assert_eq!((r.count(Outcome::Closed), r.count(Outcome::Parked), r.count(Outcome::Snoozed), r.count(Outcome::Kept)), (1, 1, 0, 1));
        assert_eq!(r.spaces_closed, 1);
        assert_eq!(run.log, [
            format!("tmux set-option -w -t @2 @review_kept {}", NOW - 20 * H),
            "run-shell -b '/s/closed-tabs.sh' close '@3'".to_string(),
            "run-shell -b '/s/stash.sh' 'stash' '@4'".to_string(),
            "run-shell -b '/s/review.sh' close-space '$3' '/dev/ttys004' '@4'".to_string(),
        ]);
    }
}
