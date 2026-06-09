# Agent tab indicator

Each tmux tab (window) reflects the state of the AI agent (Claude Code or
Codex CLI) running inside it, rendered through the Catppuccin status bar:

| State | Trigger | Tab appearance |
|---|---|---|
| *(none)* | no agent process in the window | stock Catppuccin tab |
| `idle` | agent open, not working | mauve `󰚩` robot glyph, stock colors |
| `running` | agent mid-turn | `󰚩` glyph blinking blue↔peach at 1 s (watcher toggles global `@agent_blink`), no bg change |
| `needs-input` | permission prompt / idle-wait / turn failed | `●` glyph, yellow `#f9e2af` background |
| `done` | turn finished | `●` glyph, green `#a6e3a1` background |
| any | agent has a conversation title | tab name = `project/short-title`, else `#W` |

**Seen-it semantics:** `needs-input`/`done` only tint *background* tabs. If
the event lands on the focused window — or you focus a tinted tab — the
state discharges to `idle` (`after-select-window` hook).

## Architecture

Two per-window tmux user options are the single source of truth:

- `@agent_state` — `idle | running | needs-input | done` (unset = no agent)
- `@agent_summary` — short conversation title

Three components maintain and render them:

### 1. `scripts/agent-tab-indicator.sh` (event-driven)

Invoked as `agent-tab-indicator.sh <mode> <agent>` with the hook's JSON
payload on stdin. Hook processes are children of the agent, so
`TMUX_PANE` identifies the agent's window. Wired into:

**Claude Code** (`~/.claude/settings.json`):

| Hook | Mode |
|---|---|
| `SessionStart` | `idle` (skips `source=compact` — fires mid-turn) |
| `UserPromptSubmit` | `running` |
| `PostToolUse` | `heartbeat` (re-arms `running` after an answered permission prompt; never reads stdin — `tool_response` can be huge) |
| `PermissionRequest`, `Notification` (matcher `permission_prompt\|idle_prompt`), `StopFailure` | `needs-input` |
| `Stop` | `done` |
| `SessionEnd` | `clear` (skips reasons `clear`/`resume` — a new SessionStart follows) |

Note: `Notification`'s `idle_prompt` fires only after
`messageIdleNotifThresholdMs` (default 60 s) of waiting.

**Codex** (`~/.codex/hooks.json` — native hooks; the legacy `notify` slot
stays untouched for SkyComputerUseClient): `SessionStart` (matcher
`startup|resume`) → `idle`, `UserPromptSubmit` → `running`, `PostToolUse` →
`heartbeat`, `PermissionRequest` → `needs-input`, `Stop` → `done`. Codex has
no SessionEnd; the watcher handles cleanup. **Codex requires interactive
trust approval for new hook entries** — run `codex` and accept the "New
hook — review required" prompt (or `/hooks` in the TUI). Until approved the
hooks don't fire and codex windows only get watcher-driven `idle` presence.

Subagent-context events (payload has `agent_id`) are ignored so a
subagent's Stop can't flip the main agent's tab.

**Summary sources** (best first): Claude — last `ai-title` entry in the
transcript tail (`transcript_path` from the payload), else the submitted
prompt; Codex — `thread_name` from `~/.codex/session_index.jsonl` keyed by
`session_id`, else the prompt. Sanitized (no `#`/`"`, one line, ≤60 chars).

**Tab name format**: `project/short-title`. Project is the basename of the
hook's `cwd` (`~` for `$HOME`). The raw title is condensed to its 2–4 most
identifying words by a detached `copilot -p … --model claude-haiku-4.5`
call (answer on stdout, stats footer on stderr; a plain text prompt grants
no tool permissions) and cached forever in
`~/.cache/agent-tab/titles.tsv` keyed by title hash — each distinct title
costs one haiku call ever. Until the condensation lands (or if `copilot`
fails), the tab shows `project/<raw title>` as an interim. No display-side
truncation.

### 2. `scripts/agent-tab-watcher.sh` (presence daemon)

Singleton (PID file, coffee-watcher pattern), spawned from tmux.conf,
polls every 1 s (doubling as the running-glyph blink driver — it toggles
the global `@agent_blink` option each tick while any window is running):
matches agent processes to windows by TTY (`ps -o comm`
basename `claude`/`codex`, plus the bare `N.N.N` pattern — Claude's binary
is version-named and `#{pane_current_command}` reports that, so formats
can't detect presence). Reconciles:

- agent present, no state → seed `idle`
- no agent, state set → unset both options (covers SIGKILL, `kill-pane`,
  crashes — SessionEnd is best-effort and codex has none)

Hook-set states are never overridden while the agent lives. Known
limitation: a one-shot `claude -p` exits right after `Stop`, so its `done`
tint is GC'd within ~2 s. Per-window state also means two agents in one
window share a single state (last writer wins).

### 3. Rendering (`tmux.conf`, Catppuccin v0.2.0)

Catppuccin builds `window-status-format` **once at load** from **global**
options — per-window `@catppuccin_*` overrides are impossible. Instead the
global `@catppuccin_window_default_background` / `_current_background`
options are set to a nested `#{?…}` conditional on `#{@agent_state}`.
Catppuccin pastes that string into all four tab segments that use
`$background` (number fg, middle-sep bg, text bg, right-sep fg), so the
whole tab tints consistently at render time. Constraints: the expression
must be space-free and quote-free (catppuccin's option reader splits on
spaces and strips quotes); tmux expands conditionals inside `#[…]` style
blocks (verified on tmux 3.6b).

Side effect of `fill=number`: the window NUMBER's fg is that same
expression, which would render a pastel digit on the blue/orange accent
(WCAG contrast 1.2–1.7 — illegible). The companion
`@catppuccin_window_*_color` conditionals darken the accent to crust
`#11111b` on attention states so the state-colored digit reads against it.

The text options add the state glyph, a readable fg on bright backgrounds
(crust `#11111b`), and `#{?#{n:#{@agent_summary}},#{=/24/…:#{@agent_summary}},#W}`.

`rename-window` is deliberately **not** used for titles: it disables
`automatic-rename` per window and tmux-resurrect persists both the stale
name and that flag across restores. User options aren't saved by resurrect,
so stale summaries simply vanish.

## Troubleshooting

- **Tab stuck in a state** → is the watcher alive? `cat ${TMPDIR:-/tmp}/agent-tab-watcher.$(id -u).pid`
  and `kill -0 $(cat …)`. It respawns on tmux.conf reload (`prefix r`).
- **Codex tabs only ever show idle** → hook trust not granted; run `codex`
  and approve, or check `[hooks.state]` entries in `~/.codex/config.toml`.
  (Installing the entries reserialized `hooks.json` — whitespace/`\/`
  escaping changed but existing groups kept their content and indices, so
  established trust hashes should survive; if codex unexpectedly re-prompts
  for chezmoi-guard/NotchBar, re-approving once is safe. Pre-merge backup:
  `~/.codex/hooks.json.bak-agent-tab`.)
- **No summary on a fresh Claude session** → no `ai-title` yet; the first
  prompt is used as fallback, the real title appears on later events.
- **Inspect state**: `tmux list-windows -a -F '#{window_id} #{@agent_state} #{@agent_summary}'`
- All files are chezmoi-managed: edit
  `~/.local/share/chezmoi/dot_config/tmux/…`, then `chezmoi apply`.
  (`~/.claude/settings.json` and `~/.codex/hooks.json` are *not* managed.)
