//! # agent-ui core
//!
//! The data, ordering, actions and helpers behind the agent sidebar (a
//! herdr-style WezTerm split) and the Option-W tmux popup menu. A faithful
//! port of `scripts/agent-roster.py`'s model; the UIs live in
//! `src/sidebar*.rs` and `src/menu*.rs` and build only on what is here.
//!
//! ## The loop a UI runs
//!
//! ```no_run
//! use agent_ui::{Core, Tmux, Actions};
//! let mut core = Core::new(Tmux::from_env());
//! let actions = Actions::new(core.tmux.clone());
//! let client = Some("/dev/ttys004");
//! loop {
//!     let view = core.refresh(client);         // ONE tmux call; never waits on git status
//!     // draw `view` ... then on input:
//!     // actions.go(&core.snapshot, client.unwrap(), &agent.window_id, &agent.session)
//!     std::thread::sleep(std::time::Duration::from_secs(1)); // REFRESH
//! #   break;
//! }
//! ```
//!
//! - **Cadence.** The Python refreshes once a second ([`REFRESH`]) and redraws
//!   only when the frame changed. `@agent_blink` (the pulse) is a global the
//!   watcher flips every second, read from the same snapshot, so a 1 s tick
//!   can alias against it (two frames in one phase). A UI that wants a
//!   steadier pulse may refresh at 0.5 s; the snapshot is cheap (~5 ms).
//!   Draw from [`ViewModel::blink`] only: every colour in the view
//!   ([`Agent::color`], [`Agent::flag`]) already has the phase applied, and
//!   the window the client is on never pulses. The pulse freezes when the
//!   watcher stops (it stops flipping), exactly like the tab bar.
//! - **Input between refreshes** must be resolved against the frame ON
//!   SCREEN (the Python handles queued input before redrawing): keep the
//!   last drawn [`ViewModel`]/labels/click targets until the next draw.
//! - **After a move** (go/next/back) refresh at once so the highlight
//!   follows without waiting a tick (Strip.click).
//! - **Git** ([`Space::branch`], [`Space::git`]) fills in over the first
//!   refreshes: status arrives from a background thread; call
//!   [`Core::rebuild`] (no tmux call) on the next tick to pick it up.
//!
//! ## Modules
//!
//! - [`tmux`]: the server handle ([`Tmux`], socket override
//!   `AGENT_UI_TMUX_SOCKET`), the snapshot format and its parse
//!   ([`Snapshot`], [`Window`], [`Client`]).
//! - [`model`]: state predicates and buckets ([`Cat`], [`Counts`],
//!   `is_attn`, `in_flight`, `rank`, `cat`), colours (`dot_color`, `flag`),
//!   glyphs, and [`needs_order`] (agent-jump.sh `list`, byte-faithful).
//! - [`view`]: [`ViewModel`] = needs + spaces (with agents) + parked + log +
//!   counts, and [`Core`], the read side in one struct.
//! - [`actions`]: [`Actions`] (go, next, back, goto_session, unstash, park,
//!   close, restart_watcher, agent_pane, peek), [`pick_agent_pane`],
//!   `watcher_age`.
//! - [`wezterm`]: the sidebar's marker OSC, mouse mode, client resolution
//!   and focus hand-back ([`WeztermLink`]).
//! - [`hotkeys`]: number-key labels and digit matching.
//! - [`git`]: `git_head` (pure file reads) and the [`GitCache`].
//! - [`events`]: the event log reader.
//! - [`text`]: cell widths, `clip`, `fit_label`, `ago`, `sanitize`.
//! - [`palette`]: the colour table ([`palette::HEX`], byte-identical to the
//!   Python's; cua-notch check-invariants section 65 guards it).
//! - [`ansi`]: `capture-pane -e` → styled lines for peeks.
//! - [`json`]: the small JSON value used by `dump` and `wezterm cli list`.
//! - [`collate`]: libc collation for the NEEDS YOU sort.
//! - [`proc`]: subprocesses with deadlines.

pub mod actions;
pub mod ansi;
pub mod collate;
pub mod events;
pub mod git;
pub mod hotkeys;
pub mod json;
pub mod menu;
pub mod model;
pub mod palette;
pub mod proc;
pub mod sidebar;
pub mod text;
pub mod tmux;
pub mod view;
pub mod wezterm;

pub use actions::{pick_agent_pane, Actions};
pub use collate::Collator;
pub use events::{Event, EventLog};
pub use git::{GitCache, GitStatus};
pub use model::{needs_order, Cat, Counts, Flag};
pub use tmux::{Client, Snapshot, Socket, Tmux, Window};
pub use view::{Agent, Core, LogEntry, Need, Parked, Space, ViewModel};
pub use wezterm::WeztermLink;

/// agent-roster.py `REFRESH`: one snapshot a second.
pub const REFRESH: std::time::Duration = std::time::Duration::from_secs(1);
