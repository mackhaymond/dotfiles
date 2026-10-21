//! The dialog detector on real captures (`fixtures/`: Claude Code 2.1.306 in
//! a private tmux server, `capture-pane -p`, paths shortened) and synthetic
//! ones, the keys, and the window refusals.

use super::*;
use crate::tmux::tests::row;

const BASH: &str = include_str!("fixtures/perm_bash.txt");
const WRITE: &str = include_str!("fixtures/perm_write.txt");
const NARROW: &str = include_str!("fixtures/perm_narrow.txt");
const ASK: &str = include_str!("fixtures/ask.txt");
const ANSWERED: &str = include_str!("fixtures/answered.txt");
const DENIED: &str = include_str!("fixtures/denied.txt");

#[test]
fn claude_permission_dialogs_match() {
    assert_eq!(detect(BASH), Ok(Dialog { yes: 1, no: 4 }));
    assert_eq!(detect(WRITE), Ok(Dialog { yes: 1, no: 3 }));
    // A 50-column pane: options wrap onto indented lines.
    assert_eq!(detect(NARROW), Ok(Dialog { yes: 1, no: 4 }));
    // Older layouts: the No option's "(esc)" tail, a rounded box.
    let tell = "x\n Do you want to make this edit to a.rs?\n ❯ 1. Yes\n   2. Yes, and don't ask again this session\n   \
                3. No, and tell Claude what to do differently (esc)\n";
    assert_eq!(detect(tell), Ok(Dialog { yes: 1, no: 3 }));
    let boxed = "│ Do you want to proceed?                │\n│ ❯ 1. Yes                               │\n\
                 │   2. No, and tell Claude what to do    │\n";
    assert_eq!(detect(boxed), Ok(Dialog { yes: 1, no: 2 }));
}

#[test]
fn questions_and_quiet_screens_do_not() {
    // AskUserQuestion asking the very same words, with Yes / No options.
    assert_eq!(detect(ASK), Err(Refusal::Question));
    // The prompt answered (allowed, then denied): the input box is back.
    assert_eq!(detect(ANSWERED), Err(Refusal::NoDialog));
    assert_eq!(detect(DENIED), Err(Refusal::NoDialog));
    assert_eq!(detect(""), Err(Refusal::NoDialog));
    // A plain prompt whose last reply asked in prose.
    let prose = "⏺ Do you want to proceed with the refactor?\n\n────\n❯ \n────\n  ⏸ manual mode on\n";
    assert_eq!(detect(prose), Err(Refusal::NoDialog));
}

#[test]
fn unknown_layouts_are_refused() {
    // The dialog is still in the scrollback but the input box is under it.
    let under = format!("{BASH}\n────\n❯ \n────\n");
    assert_eq!(detect(&under), Err(Refusal::Unknown));
    // No plain Yes (only the persistent one): never pick it.
    let always = " Do you want to proceed?\n ❯ 1. Yes, and don't ask again\n   2. No\n";
    assert_eq!(detect(always), Err(Refusal::Unknown));
    // No last option is not a No; options out of order.
    let no_no = " Do you want to proceed?\n ❯ 1. Yes\n   2. Maybe\n";
    assert_eq!(detect(no_no), Err(Refusal::Unknown));
    let order = " Do you want to proceed?\n ❯ 1. Yes\n   3. No\n";
    assert_eq!(detect(order), Err(Refusal::Unknown));
    // A question with no options under it.
    assert_eq!(detect(" Do you want to proceed?\n\n"), Err(Refusal::NoDialog));
}

#[test]
fn keys_for_each_verdict() {
    let d = Dialog { yes: 1, no: 4 };
    assert_eq!(keys(d, Verdict::Allow), ["1"]);
    assert_eq!(keys(d, Verdict::Deny), ["Escape"]);
    assert_eq!(keys(Dialog { yes: 2, no: 3 }, Verdict::Allow), ["2"]);
    assert_eq!((Verdict::parse("allow"), Verdict::parse("deny"), Verdict::parse("yes")),
        (Some(Verdict::Allow), Some(Verdict::Deny), None));
}

/// Codex, OpenCode, an unknown kind and a non-perm detail are refused before
/// any pane is read; a gone window too. (The server does not exist, so a
/// window that passed would fail at its pane, never send.)
#[test]
fn windows_refused_up_front() {
    let snap = Snapshot::parse(&[
        row("main", 1, "@1", &[("state", "needs-input"), ("kind", "codex"), ("detail_kind", "perm"), ("summary", "Cx")]),
        row("main", 2, "@2", &[("state", "needs-input"), ("kind", "claude"), ("detail_kind", "ask"), ("summary", "Asks")]),
        row("main", 3, "@3", &[("state", "needs-input"), ("kind", "opencode"), ("detail_kind", "perm"), ("summary", "Oc")]),
        row("main", 4, "@4", &[("state", "needs-input"), ("kind", "claude"), ("detail_kind", "perm"), ("summary", "Ok")]),
    ].join("\n"));
    let a = Actions::new(Tmux::with_socket(Some(crate::tmux::Socket::Name(format!("agentui-answer-none-{}", std::process::id())))));
    assert_eq!(answer(&a, &snap, "@1", Verdict::Allow, None), Err("Cx: answer Codex prompts in its tab".into()));
    assert_eq!(answer(&a, &snap, "@2", Verdict::Allow, None), Err("Asks: no permission prompt pending".into()));
    assert_eq!(answer(&a, &snap, "@3", Verdict::Deny, None), Err("Oc: not a Claude agent".into()));
    assert_eq!(answer(&a, &snap, "@9", Verdict::Deny, None), Err(PANE_GONE.into()));
    assert_eq!(answer(&a, &snap, "@4", Verdict::Allow, None), Err(PANE_GONE.into()));
}

/// A click or key names the prompt it saw; when the window's prompt is
/// another one by now (a new `@agent_since`, another detail) nothing runs.
#[test]
fn another_prompt_is_refused() {
    let snap = Snapshot::parse(&row("main", 1, "@1", &[("state", "needs-input"), ("kind", "claude"),
        ("detail_kind", "perm"), ("summary", "P"), ("detail", "Bash  ls\x01"), ("since", "500 needs-input")]));
    let a = Actions::new(Tmux::with_socket(Some(crate::tmux::Socket::Name(format!("agentui-answer-none2-{}", std::process::id())))));
    let ag = Agent::from_window(&snap.windows[0], false, None, 600.0);
    let key = prompt_key(&ag);
    assert_eq!(key, format!("500 {}", crate::text::sanitize("Bash  ls\x01")));
    // The prompt the card showed: it gets as far as the (absent) pane.
    assert_eq!(answer(&a, &snap, "@1", Verdict::Allow, Some(&key)), Err(PANE_GONE.into()));
    for stale in ["499 Bash ls", "500 Bash cat", ""] {
        assert_eq!(answer(&a, &snap, "@1", Verdict::Allow, Some(stale)), Err("P: that prompt changed · look again".into()), "{stale}");
    }
}

/// The select's `❯` must be on the plain Yes: anywhere else (an editable
/// "don't ask again" row, No) a digit could land in a text field.
#[test]
fn pointer_must_be_on_yes() {
    let moved = BASH.replace(" ❯ 1. Yes", "   1. Yes").replace("   2. Yes, and always", " ❯ 2. Yes, and always");
    assert_eq!(detect(&moved), Err(Refusal::Pointer));
    let none = BASH.replace(" ❯ 1. Yes", "   1. Yes");
    assert_eq!(detect(&none), Err(Refusal::Pointer));
    let on_no = BASH.replace(" ❯ 1. Yes", "   1. Yes").replace("   4. No", " ❯ 4. No");
    assert_eq!(detect(&on_no), Err(Refusal::Pointer));
}

/// A target is settled once it has sat at a cell SETTLE, whatever redraws
/// came between; another target there (or none) starts that cell over.
#[test]
fn clicks_settle_per_target_and_cell() {
    let t0 = std::time::Instant::now() - Duration::from_secs(10);
    let at = |ms: u64| t0 + Duration::from_millis(ms);
    let mut a = Arrivals::default();
    a.update([((1, 1), 'a'), ((2, 1), 'a')], at(0));
    // Redrawn every 125 ms with the same target: the clock keeps running.
    for k in 1..=3 {
        a.update([((1, 1), 'a'), ((2, 1), 'a')], at(125 * k));
    }
    assert!(!a.settled((1, 1), &'a', at(399)));
    assert!(a.settled((1, 1), &'a', at(400)));
    assert!(!a.settled((1, 1), &'b', at(900)));
    assert!(!a.settled((5, 5), &'a', at(900)));
    // Another target takes the cell at 500: its own clock.
    a.update([((1, 1), 'b'), ((2, 1), 'a')], at(500));
    assert!(!a.settled((1, 1), &'b', at(800)) && a.settled((1, 1), &'b', at(900)));
    assert!(a.settled((2, 1), &'a', at(800)));
    // Gone for a frame: starts over when it comes back.
    a.update([((1, 1), 'b')], at(1000));
    a.update([((1, 1), 'b'), ((2, 1), 'a')], at(1100));
    assert!(!a.settled((2, 1), &'a', at(1300)));
}

#[test]
fn format_literals() {
    assert_eq!(fmt_literal("Bash a,b {x} }y ~/#").as_deref(), Some("Bash a#,b {x#} #}y ~/##"));
    assert_eq!(fmt_literal(""), Some(String::new()));
    assert_eq!(fmt_literal("a\nb"), None);
}

/// The state rides with the capture; the send is one `if-shell -F` on that
/// same state, copy / view mode cancelled inside it, other modes and
/// synchronized panes refused.
#[test]
fn one_guarded_send() {
    assert_eq!(PaneState::parse("12,340,0,,0\n"), Some(PaneState { cursor_y: 12, history: 340, in_mode: false, mode: "".into(), sync: false }));
    assert_eq!(PaneState::parse("3,0,1,copy-mode,1"), Some(PaneState { cursor_y: 3, history: 0, in_mode: true, mode: "copy-mode".into(), sync: true }));
    assert_eq!(PaneState::parse("x,0,0,,0"), None);
    assert_eq!(PaneState::parse("1,2,0"), None);
    let d = Dialog { yes: 1, no: 4 };
    let st = PaneState::parse("12,340,0,,0").unwrap();
    let asked = Asked { since: "1792597119 needs-input", detail: "Bash a,b}" };
    let cond = "#{&&:#{==:#{cursor_y},12},#{&&:#{==:#{history_size},340},#{&&:#{==:#{pane_in_mode},0},\
                #{&&:#{==:#{synchronize-panes},0},#{&&:#{==:#{@agent_since},1792597119 needs-input},\
                #{==:#{@agent_detail},Bash a#,b#}}}}}}}";
    assert_eq!(plan(&st, d, Verdict::Allow, "%7", asked).unwrap(),
        ["if-shell", "-F", "-t", "%7", cond, "send-keys -t %7 1 ; display-message -p sent", "display-message -p changed"]);
    assert_eq!(plan(&st, d, Verdict::Deny, "%7", asked).unwrap()[5], "send-keys -t %7 Escape ; display-message -p sent");
    let copy = PaneState { in_mode: true, mode: "copy-mode".into(), ..st.clone() };
    let p = plan(&copy, d, Verdict::Allow, "%7", asked).unwrap();
    assert!(p[4].contains("#{==:#{pane_in_mode},1}"), "{}", p[4]);
    assert_eq!(p[5], "send-keys -X -t %7 cancel ; send-keys -t %7 1 ; display-message -p sent");
    assert!(plan(&PaneState { mode: "view-mode".into(), ..copy.clone() }, d, Verdict::Deny, "%7", asked).is_ok());
    assert_eq!(plan(&PaneState { mode: "tree-mode".into(), ..copy }, d, Verdict::Allow, "%7", asked), Err("its pane is in a tmux mode"));
    assert_eq!(plan(&PaneState { sync: true, ..st.clone() }, d, Verdict::Allow, "%7", asked), Err("its window has synchronize-panes on"));
    assert_eq!(plan(&st, d, Verdict::Allow, "%7", Asked { detail: "a\nb", ..asked }), Err("can't pin down its prompt"));
}

#[test]
fn answerable_cards() {
    let agent = |set: &[(&str, &str)]| Agent::from_window(&Window::parse(&row("main", 1, "@1", set)).unwrap(), false, None, 0.0);
    assert!(answerable(&agent(&[("state", "needs-input"), ("kind", "claude"), ("detail_kind", "perm")])));
    // Answered (idle, the perm detail kept), a question, Codex.
    assert!(!answerable(&agent(&[("state", "idle"), ("kind", "claude"), ("detail_kind", "perm")])));
    assert!(!answerable(&agent(&[("state", "needs-input"), ("kind", "claude"), ("detail_kind", "ask")])));
    assert!(!answerable(&agent(&[("state", "needs-input"), ("kind", "codex"), ("detail_kind", "perm")])));
}
