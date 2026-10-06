# Yabai + Skhd + Karabiner-Elements + WezTerm: Complete Setup Reference

> *Docs synced to config: **2026-10-06** — after the full-system review fixes (`452312d`) and the unresolved-pin resolver (`473a7e8`/`4f08c37`): labeled pin rules re-registered by refresh, conservative label-follows-app, fullscreen-aware positional labels, resolver guards + layout/float restore, startup lock ownership, `sudo -n --load-sa`; plus doc drift (Karabiner fires the binds, BetterTouchTool → Arc rules in `karabiner-base.json`, `signal --list` works, v7 SA flags, `com.asmvik.yabai`). Previous audit **2026-08-21** — multi-agent review of the stray-float heal (`unfloat_pins`) for conventions, runtime interaction, and source-claim accuracy. Findings folded in: the repair is a view rebuild rather than a cross-space bounce, spaces are addressed by label, non-standard windows are excluded from both the repair and the settle predicate, and the pinned-home map moved into `yabai_common.sh`. Earlier audit 2026-06-06 covered the bsp keybind set + the event-driven self-heal (0 critical/high; no duplicate or then-BetterTouchTool-reserved `hyper+a`/`hyper+s` binds). Pinned-window guards intact — float direction only, since 2026-08-21.*

## 1. Overview & Mental Model

This is a single-laptop-first tiling window manager setup optimized for seamless occasional multi-display work. The system treats the built-in MacBook display as the permanent "master" home for all work, with an optional external display as a temporary "external" work surface that comes and goes via dock.

**Core Mental Model:**

- **Master display (laptop)**: Always present, hosts all 10 canonical labeled workspaces (terminal, main, school, todo, schedule, mail, calendar, messages, ai, agent). Stable reference point.
- **External display**: Optional. Comes up empty-and-ready when docked; user manually pushes workspaces there (or pulls them back home). Automatically healed on undock.
- **Labeled spaces, not indexed**: Spaces are identified by stable **labels** (not fragile array indices), because macOS renumbers indices whenever Mission Control is touched or displays change. All bindings, rules, and scripts use labels as the canonical reference.
- **Stack layout**: Only one window visible at a time per space; other windows are hidden in a z-order stack. Navigate with `hyper+z` (next) / `hyper+x` (prev).
- **Hyper modifier**: Caps Lock remapped (via Karabiner) to Cmd+Ctrl+Opt+Shift; this unified modifier powers nearly all window-manager shortcuts.

## 2. Where Everything Lives

### File Map: Configuration Sources & Targets

| Source (Chezmoi) | Target (Deployed To) | Type | Purpose |
|---|---|---|---|
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/yabai/executable_yabairc` | `~/.config/yabai/yabairc` | Executable shell script | Window manager core: signals, rules, app pinning, layout |
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/skhd/skhdrc.tmpl` | `~/.config/skhd/skhdrc` | Template config | Declarative bind list (hyper+key → yabai commands); compiled into `karabiner.json`, which fires them (the skhd daemon is retired) |
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/wezterm/wezterm.lua.tmpl` | `~/.config/wezterm/wezterm.lua` | Template config | Terminal emulator: startup, keybindings, tmux integration |
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/private_karabiner/modify_private_karabiner.json.tmpl` | `~/.config/karabiner/karabiner.json` | chezmoi `modify_` template (owns the whole file) | Builds `karabiner.json` = hand-written `.chezmoitemplates/karabiner-base.json` (caps_lock→hyper, the F13/F14/F18/F19 chords, Arc's hyper+a/s/c) + the skhdrc binds compiled by `dot_config/skhd/executable_skhd-to-karabiner.py` |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_workspace.sh` | `~/code/various_scripts/yabai_workspace.sh` | Executable script | Focus workspace by label |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_send_window.sh` | `~/code/various_scripts/yabai_send_window.sh` | Executable script | Move focused window to space and follow focus (respects pinned homes) |
| `…/code/various_scripts/executable_yabai_send_window_external.sh` | `~/code/various_scripts/yabai_send_window_external.sh` | Executable script | Fling the focused unpinned window to the external's on-demand `ext` scratch-work space (create + follow). See the "External scratch-work space" design note |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_workspace_refresh.sh` | `~/code/various_scripts/yabai_workspace_refresh.sh` | Executable script | Reconcile canonical labels, refresh display topology cache |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_display.sh` | `~/code/various_scripts/yabai_display.sh` | Executable script | Focus display by name (master/external) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_space_move.sh` | `~/code/various_scripts/yabai_space_move.sh` | Executable script | Push/pull spaces between displays |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_displays.sh` | `~/code/various_scripts/yabai_displays.sh` | Executable script | Hotplug handler: dock/undock logic |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_skhd_mode.sh` | `~/code/various_scripts/yabai_skhd_mode.sh` | Executable script | Toggle space layout (bsp ↔ stack) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_toggle_float.sh` | `~/code/various_scripts/yabai_toggle_float.sh` | Executable script | Toggle the focused window's float (`hyper+t`); both stack & bsp; refuses to *float* a pinned app on its home space, always allows *un*-floating |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_float_borders.sh` | `~/code/various_scripts/yabai_float_borders.sh` | Executable script | Draw a JankyBorders border around floating windows (drives the `borders` daemon whitelist; runs only while something floats) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_skhd_stack_next.sh` | `~/code/various_scripts/yabai_skhd_stack_next.sh` | Executable script | **`hyper+z`, layout-aware:** STACK → next stack layer; BSP → mirror tree horizontally (`--mirror x-axis`) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_skhd_stack_prev.sh` | `~/code/various_scripts/yabai_skhd_stack_prev.sh` | Executable script | **`hyper+x`, layout-aware:** STACK → previous stack layer; BSP → mirror tree vertically (`--mirror y-axis`) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_mouse_follow.sh` | `~/code/various_scripts/yabai_mouse_follow.sh` | Executable script | Warp cursor to newly focused display |
| `…/code/various_scripts/executable_yabai_heal.sh` | `~/code/various_scripts/yabai_heal.sh` | Executable script | **Debounced self-heal** — coalesce `space_destroyed` / `mission_control_exit` into one `yabai_workspace_refresh` (single-flight mkdir lock + settle) |
| `…/code/various_scripts/executable_yabai_startup_reconcile.sh` | `~/code/various_scripts/yabai_startup_reconcile.sh` | Executable script | **Login-race + stray-float fix** — `startup` (backgrounded from yabairc): clears stale locks, re-loads the SA + polls (re-apply rules / re-pin Arc / un-float misclassified pinned windows) until pinned apps are on their home spaces **and tiled**, restores bsp layouts + `hyper+t` floats after an automatic restart, then hands off to `yabai_pin_resolve.sh`. `float [--dry-run]`: the one-pass float sweep run by 3 signals. Single-flighted (pid-owned locks) |
| `…/code/various_scripts/executable_yabai_pin_resolve.sh` | `~/code/various_scripts/yabai_pin_resolve.sh` | Executable script | **Unresolved-pin heal** — guarded `yabai --restart-service` when a pinned app's window is listed by yabai but un-actionable (see "Unresolved pinned windows"). Log `~/Library/Logs/yabai/resolve.log` |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_screen_flash.sh` | `~/code/various_scripts/yabai_screen_flash.sh` | Executable script | **DISABLED (dormant)** — was the external-display focus border flash; the signal was removed 2026-06-04 |
| `…/code/various_scripts/yabai_screen_flash.js` | `~/code/various_scripts/yabai_screen_flash.js` | JXA helper | **DISABLED (dormant)** — drew the border overlay for `yabai_screen_flash.sh` |
| `…/code/various_scripts/executable_yabai_reorder_spaces.sh` | `~/code/various_scripts/yabai_reorder_spaces.sh` | Executable script | Keep labeled spaces in canonical order per display |
| `…/code/various_scripts/executable_yabai_fullscreen_focus.sh` | `~/code/various_scripts/yabai_fullscreen_focus.sh` | Executable script | Focus the Nth native-fullscreen app (`hyper+3-9`), WezTerm excluded |
| `…/code/various_scripts/executable_yabai_terminal_follow.sh` | `~/code/various_scripts/yabai_terminal_follow.sh` | Executable script | Keep `terminal` label on WezTerm in/out of fullscreen; sweep husk spaces |
| `…/code/various_scripts/yabai_common.sh` | `~/code/various_scripts/yabai_common.sh` | Sourced shell lib (not executable) | **Shared helper** — single source of the master-display UUID, the canonical `YABAI_LABELS` list, the agent apps (`YABAI_AGENT_APPS`/`_RE`), the pinned-home map (`YABAI_PINNED_HOMES`, `YABAI_PINNED_APPS_RE`) and the rules generated from it (`yabai_pin_rules_add`), `yabai_home_map_json()`, `yabai_spaces_json()`, the jq filters `YABAI_JQ_PIN_ELIGIBLE` / `YABAI_JQ_PIN_UNRESOLVED`, the state/log dirs (`YABAI_STATE_DIR`, `YABAI_LOG_DIR`) + `yabai_log`, `yabai_master_index()` (UUID-then-area resolver), `yabai_load_cache()`, and the `ext` helpers. Sourced by yabairc and ~14 `yabai_*` scripts; required sibling |
| `…/dot_hammerspoon/init.lua` | `~/.hammerspoon/init.lua` | Lua config | **Hammerspoon**: classify Arc windows via AXIdentifier; pin the two main windows to main/school (Little Arc left managed). Required dependency, launches at login |
| (unmanaged) `~/code/projects/layerbar` → `~/Applications/LayerBar.app` | LaunchAgent `com.mackhaymond.layerbar` | Native menu bar app | **LayerBar** — the **live** status indicator. Shows current/total stack layer (e.g. `2 / 3`), or `BSP`/`FLOAT`. Refreshed instantly by the yabai signals poking `notifyutil -p com.mackhaymond.layerbar.refresh`; queries yabai over its unix socket. Read-only/passive (no window-manager effect) |
| `…/swiftbar_plugins/executable_yabai_layers.30s.sh` | `~/swiftbar_plugins/yabai_layers.30s.sh` | Executable plugin | **Dormant/superseded** SwiftBar version of the layer indicator (ignored via `.swiftbarignore`). Kept as the fallback if LayerBar is ever uninstalled |
| `…/dot_config/sketchybar/{items,plugins}/yabai_layers.sh` | `~/.config/sketchybar/{items,plugins}/yabai_layers.sh` | Shell scripts | **Dead/superseded** sketchybar version of the same layer indicator. sketchybar is **not running**. Kept in-tree only as a dormant alternative |
| `…/code/various_scripts/executable_restart-yabai.sh` | `~/code/various_scripts/restart-yabai.sh` | Executable script (Raycast) | Soft-restart yabai (`yabai --restart-service`), invoked from Raycast. Not wired into any signal/bind |

### Shared State: Display Topology Cache

**Path:** `~/.cache/yabai/workspace_cache.env` (default; override via `$YABAI_WORKSPACE_CACHE` env var)

**Canonical Contents:**
```bash
DISPLAY_COUNT=<0, 1, or 2>
MASTER_DISPLAY_INDEX=<usually 1>
EXTERNAL_DISPLAY_INDEX=<index or empty>
MASTER_DISPLAY_UUID=37D8832A-2D66-02CA-B9F7-8F30A301B230
```

**Purpose:** Sourced by all workspace scripts to avoid expensive repeated `yabai -m query --displays` calls. Written atomically by `yabai_workspace_refresh.sh` and `yabai_displays.sh`.

## 3. Component Deep-Dives

### 3.1 Yabai Core Configuration (yabairc)

**File:** `~/.config/yabai/yabairc`

#### Layout & Global Knobs

| Setting | Value | Purpose |
|---------|-------|---------|
| `layout` | `stack` | Single window visible; others stacked and hidden. Navigate with hyper+z/x. |
| `window_origin_display` | `focused` | New windows inherit the focused display (not the app's launch display). |
| `display_arrangement_order` | `horizontal` | External display to the right of laptop. |
| `window_shadow` | `off` | No drop shadow on borders. |
| `top_padding` | `0` | No padding above first window; macOS menu bar handles offset. |

#### Window Rules: Unmanaged Apps

These applications do not participate in yabai's tiling; they float freely:

**System utilities:** System Settings, Calculator, BetterTouchTool, Karabiner-Elements  
**Monitoring:** Activity Monitor, DaisyDisk, iStat Menus  
**Other:** Mail, Finder, Steam, BetterZip, Python REPL windows, DevPod, Setapp, Permute, TI-Nspire, LiveMath Maker, Antidote

#### App-to-Space Pinning Rules

These apps automatically appear on their designated space when launched. The rules are **generated**, not hand-written: `yabai_pin_rules_add` (in `yabai_common.sh`) registers one labeled rule per app (`pin_<app>`, e.g. `pin_Todoist`, plus `pin_agent`) from `YABAI_PINNED_HOMES` + `YABAI_AGENT_APPS`, so the rules and the pinned-home guards in the scripts can't drift apart.

| App | Target Space |
|-----|--------------|
| wezterm-gui, WezTerm | terminal |
| Todoist | todo |
| Granola | schedule |
| Spark Mail | mail |
| Notion Calendar | calendar |
| Messages | messages |
| ChatGPT | ai |
| Claude | ai |
| Conductor, Claudia, OpenChamber, OpenCode, Jean, T3 Code (Alpha), Muse | agent |

**`agent` (hyper+esc) — the generic coding-agent view.** Unlike every other label,
`agent` is not one app's home: it is a *view* that whichever coding-agent GUI is
running lands on (Conductor is the primary). The app set is defined ONCE, as
`YABAI_AGENT_APPS` in `yabai_common.sh`, and is consumed by the `pin_agent`
rule (`yabai_pin_rules_add`), the refresh script's label assignment, the pinned-home
guards in `yabai_send_window{,_external}.sh` / `yabai_toggle_float.sh` (two-way in
the send scripts, float-direction-only in `yabai_toggle_float.sh`), and the startup
reconciler's home map via `yabai_home_map_json()` — **add an app there and nowhere else.** Because the
space can legitimately be empty, `f18` (focus `agent`) is the one *conditional*
focus bind: with no windows on the space it does nothing rather than dumping you on
an empty desktop. It replaced the old single-app `codex` space, which went stale
when Codex.app was uninstalled.

**Arc (browser):** *not* a yabai rule (the two main windows go to two different
spaces, and Little Arc is indistinguishable to yabai). The two main Arc windows
are pinned to `main`/`school` by **Hammerspoon** (`dot_hammerspoon/init.lua`) via
AXIdentifier; Little Arc stays managed. See the "Arc window pinning" design note.

**Why label-based queries?** When yabai queries spaces, it uses labels (e.g., `yabai -m query --spaces --space terminal`) rather than indices. This makes all rules robust to index renumbering caused by Mission Control or display hotplug.

> **Caveat — `space=` *rules* bind to a space, not to the label.** yabai resolves `space=<label>` to that space's **ID** when the rule is *added* (`parse_space_selector` → `effects.sid`), not on each apply; `yabai -m rule --list` therefore shows a number, not the label. Because it's the space's ID (not its index), the rule follows that space through reorders and display moves. What used to break it: a labeled space destroyed (merged by a fullscreen collapse, Mission Control) and the label re-created by `yabai_workspace_refresh.sh` on *another* space — the rule kept pointing at the dead ID and that app silently stopped pinning until a yabai restart. Fixed 2026-10-06: the rules are labeled (`pin_<app>`, and `rule --add` replaces a same-label rule), and refresh calls `yabai_pin_rules_add` again after relabeling, then `rule --apply`. A rule whose label is missing at that moment fails to parse and leaves the old one in place. WezTerm additionally gets the `window_created` nudge, which re-queries `--space terminal` live.

#### Signal Handlers

**1. `dock_did_restart`** → `sudo -n yabai --load-sa`. Reloads the scripting addition (needed for native-fullscreen, space create/destroy, etc.) after the Dock restarts. `-n` (since 2026-10-06, also on the startup load): never prompt — a stale sudoers pin used to fall through to a Touch ID dialog that blocked the rest of yabairc; now it just fails fast.

**2. `window_created`**
- **All windows:** posts the LayerBar refresh (`notifyutil -p com.mackhaymond.layerbar.refresh`) — a new window may add a stack layer.
- **WezTerm:** if a *normal* WezTerm lands on the wrong space, move it to terminal. A *fullscreen* WezTerm is left alone (guarded by `is-native-fullscreen`) so it isn't yanked out of fullscreen. *(That guard was dead until 2026-10-06: its single-quoted jq filter closed the single-quoted `action=` string, so it ran as a jq compile error. Now double-quoted.)*
- **Arc:** call `hs -c "arcSync()"` (Hammerspoon re-pins the Arc main windows; Little Arc untouched).
- **Other apps:** left wherever they land. The terminal space is **not** reserved — any window may share it with WezTerm (the old non-WezTerm "bounce" was removed).

**3. `space_changed`** → posts the LayerBar refresh, then `yabai_terminal_follow.sh`. The follow hook keeps the `terminal` label pinned to WezTerm wherever it roams (including in/out of a native-fullscreen Space), reorders, and sweeps surplus empty husk spaces. Cheap no-op when WezTerm hasn't moved. (It no longer re-activates WezTerm — removed in `815fc8e`.)

**4. `application_launched`** → `yabai -m rule --apply` (re-pins Todoist/Messages/etc.) **and** `hs -c "arcSync()"` (re-pins the Arc main windows) — one consistent "snap" moment.

**5. `display_added` (label `workspace_display_added`)** → `yabai_displays.sh added`: debounced hotplug; settles display count and refreshes cache (non-destructive; external comes up empty).

**6. `display_removed` (label `workspace_display_removed`)** → `yabai_displays.sh removed`: pulls all labeled spaces home to the master display.

**7. `display_changed` (label `mouse_follow_display`)** → `yabai_mouse_follow.sh`: warps cursor to the newly focused display.

**8. `space_destroyed` (label `heal_space_destroyed`)** and **`mission_control_exit` (label `heal_mission_control`)** → `yabai_heal.sh` → `yabai_workspace_refresh.sh`. Event-driven self-heal: a destroyed/merged labeled space (the classic label-drop — e.g. a fullscreen collapse merging an adjacent space) or a Mission Control exit (it renumbers/merges spaces) reconciles the canonical labels + order. `yabai_heal.sh` single-flights + settles (mkdir lock, `YABAI_HEAL_SETTLE`, default 0.4s) so a burst (a Mission Control session churning several spaces) heals exactly **once**. refresh is idempotent (~0.34s) and these events are infrequent → no idle/poll cost. **Not** hooked to `space_created` (refresh may create a space → would self-trigger) or `window_destroyed` (too noisy; window closes don't drop labels).

**9. ~~`display_changed` (label `flash_external_display`)~~** → **DISABLED** (2026-06-04, user request). The external-display border flash signal was removed from `yabairc`. The helper scripts `yabai_screen_flash.sh` / `.js` remain in `code/various_scripts` as dormant; re-enable by restoring the `YABAI_SCREEN_FLASH` env var + a `display_changed` signal calling it.

**10. `window_created` (label `border_sync_created`)** and **`window_destroyed` (label `border_sync_destroyed`)** → `yabai_float_borders.sh sync`. **Float-window borders** (2026-06-09): reconcile the JankyBorders (`borders`) daemon's `whitelist` to the set of apps that currently have a floating window — starting the daemon (subtle white: focused `0xffffffff` / unfocused `0xff5c6370`, round, width 2) when the first floater appears and killing it when the last one goes away (so it runs only while something floats — not a brew service). A plain `--toggle float` (hyper+t) emits **neither** event, so `yabai_toggle_float.sh` calls `sync` itself; a one-shot startup `sync` borders any floater macOS restored at login. **Scope = all `is-floating` windows**, which includes manage=off apps (Finder, System Settings, …) — yabai exposes no per-window "managed" flag to exclude them, so they're bordered whenever open. **Caveat (inherent to JankyBorders):** it borders by *app*, not *window*, so an app with both a floating and a tiled window gets the border on both. Requires `brew install felixkratz/formulae/borders`; the script degrades to a no-op if `borders` is absent.

**11. Float sweep — `space_changed` (label `float_sweep_space_changed`), `window_deminimized` (`float_sweep_deminimized`), `window_focused` filtered to the pinned apps (`float_sweep_focused`, `app=$YABAI_PINNED_APPS_RE`)** → `YABAI_EVENT=<event> yabai_startup_reconcile.sh float`: one `unfloat_pins()` pass; also hands an unresolved pinned window to `yabai_pin_resolve.sh`. See "the float sweep" below.

**12. `application_launched` filtered to the pinned apps (label `pin_resolve_launched`)** → `yabai_pin_resolve.sh application_launched --watch 30`, and **`system_woke` (label `pin_resolve_woke`)** → `yabai_pin_resolve.sh system_woke --delay 3`. See "Unresolved pinned windows" below.

**13. `window_focused` (label `layers_refresh_focus`)** and **`window_destroyed` (label `layers_refresh_destroyed`)** → `$YABAI_LAYERS_REFRESH` (the LayerBar `notifyutil` post). Focus covers stack-layer cycling and focus landing on a window of a different float/stack type; destroy covers a close lowering the stack depth. Opens and space switches refresh inline in #2/#3; the bsp↔stack toggle has no signal, so `yabai_skhd_mode.sh` posts it itself.

*(Also: a one-shot startup sync — `"$YABAI_WORKSPACE_REFRESH" startup` — runs near the **top** of yabairc, before the rules. Specific line numbers are intentionally omitted here — they drift; grep the signal name in `yabairc`.)*

**Startup reconciliation** (`yabai_startup_reconcile.sh`, run **backgrounded** right after the startup `rule --apply`): fixes the **login race** where pinned apps land on the wrong space. At login, macOS restores app windows around when yabai starts, so windows created before the signals registered get no `window_created`/`application_launched` event, and the one-shot `rule --apply` can run *before* those windows exist (or before `--load-sa` finishes — window→space moves need the scripting addition). The reconcile re-loads the scripting addition once (`sudo -n`), then **polls until stable** — repeatedly re-applying the `space=` rules, re-pinning Arc, and un-floating misclassified pins until every *running* pinned app is on its home space **and not flagged FLOAT** (see the next section), or a hard cap (`YABAI_RECONCILE_CAP`, default ~90 s). This self-truncates on a fast login (exits in ≈0.2 s once everything's home) and self-extends for slow-launching apps (Electron ChatGPT, Claude, Notion Calendar; the native Messages), so it's more robust than a fixed ramp that could miss an app finishing after the last pass. Backgrounded so it never blocks startup; single-flighted (mkdir lock, like `yabai_heal.sh`) so repeated restarts don't stack overlapping polls; idempotent. Supersedes the old workaround of manually restarting yabai after login.

The same poll also fixes a **second startup failure — a pinned app landing *floating*** (added 2026-08-21, after Claude came up floating on `ai` while ChatGPT tiled normally on the same space). yabai classifies every window it finds **before** the config runs (`window_manager_begin()` precedes `exec_config_file()`), so no rule can influence that pass, and it flags `WINDOW_FLOAT` on any window it momentarily cannot move — which a window still settling after a restore can be. Nothing clears that flag afterwards: `rule --apply` re-pins the *space* and leaves the window floating. Two traps make this harder than it looks, both verified live:

- **`manage=on` on the `space=` rules is NOT the fix.** It does suppress the misclassification for windows created while yabai is already running (the FLOAT classification is skipped for `WINDOW_RULE_MANAGED`), but it cannot help at startup — the rules don't exist yet during discovery — and it makes `rule --apply` (fired at startup *and* on every `application_launched`) take the un-float path below from whatever space happens to be active. That trades a floating window for a corrupted cross-space stack.
- **yabai re-tiles an un-floated window onto the ACTIVE space, not the window's own space** (`window_manager_make_window_floating()` ends in `space_manager_tile_window_on_space(sm, window, space_manager_active_space())`). Un-floating a window that lives on a background space silently registers it in the active space's view — two windows on different spaces sharing one stack, and the window that owned that view pushed out. Observed: Claude on `ai`, un-floated while `terminal` was active, joined WezTerm's view — **WezTerm and Claude as stack-index 1 and 2 of the same view**, with ChatGPT ejected to 0.

So `unfloat_pins()` un-floats with `--toggle float` and then, **only when the window's home space is not the focused one**, rebuilds that space's view with `--layout bsp` → `--layout stack`, addressed **by label**. The flip re-derives the tree from actual window→space membership, which both re-homes the window and drops the stale registration from the other view. Verified 2026-08-21: with `terminal` focused, a corrupted `todo` came back correct by flipping `todo` *alone*. When home **is** the focused space the un-float already landed correctly and nothing else is done — a flip there would only churn the view the user is looking at.

The flip costs one thing: it re-derives a bsp tree from scratch, losing hand-tuned splits. So a window whose home space is bsp *and* not focused is **skipped** rather than repaired — left floating, the benign failure. Every space here is `stack`, so that path is currently dead.

> **Rejected: bouncing the window through another space and back.** This was implemented first and is wrong here. It needs the scripting addition, which may not be loaded yet at login — `window --space` then fails **silently and exit-0** (yabai has no scripting-addition error string on that path), so the failure is undetectable and the window is left cross-registered while the poll declares success. yabai also **refuses** to move a window into a native-fullscreen space, which strands WezTerm off `terminal` whenever `yabai_terminal_follow.sh` has moved that label onto a fullscreen Space. The outbound leg hands focus to another window when the source space is visible (`window_manager.c:2099`). And it parks the window somewhere `yabai_workspace_refresh.sh`'s label-follows-app logic can see and re-label mid-flight. The flip touches no window and needs no scripting addition.

Two more properties worth knowing. **Which windows are eligible:** only `AXStandardWindow` root windows that aren't minimized, hidden, sticky, scratchpad, or native-fullscreen — yabai floats plenty of windows on purpose (a settings sheet, a palette) and force-tiling those would fight it, the same filter `yabai_workspace_refresh.sh`'s `space_for_app()` already applies. `can-move` is deliberately *not* filtered: a window yabai can't move right now is the transient case this exists for, so it stays a candidate, its dropped un-float is caught by the verify, and the poll retries it up to `MAX_ATTEMPTS` (10, ≈20 s). **Why the settle predicate doesn't re-derive any of that:** `unfloat_pins()` leaves `PENDING_FLOATS` = how many windows it still intends to retry, and `pins_settled()` just reads the tally. Deriving eligibility twice is how a predicate and a repair drift apart, and a window the repair will never touch holding the poll open — re-running `rule --apply` + `arcSync()` every 2 s for 90 s — is the expensive kind of drift.

Finally, un-floating changes the float set without emitting `window_created`/`window_destroyed`, so it calls `yabai_float_borders.sh sync` afterwards — the same gap `yabai_toggle_float.sh` covers after `hyper+t` — gated on an actual change so a stubborn window can't fire ~45 of them. **Arc is deliberately out of scope**: it has no `space=` rule (arcSync pins it), so an Arc window yabai flags FLOAT is only recoverable with `hyper+t`.

**Why the startup poll alone was not enough — and the float sweep (added 2026-08-29).** On 2026-08-28 Claude came up floating on `ai` after a `yabai --restart-service` 6 s after wake, and *stayed* floating for 34 h. `log show` reconstructed it exactly: the reconcile ran (its `sudo -n --load-sa` is there), fired `arcSync()` **11 times** at 2.2 s cadence — `MAX_ATTEMPTS` + 1 — then declared itself settled and exited. Ten `--toggle float`s, ten silent drops. The yabai source (v7.1.25) explains why retrying at all was hopeless: `can_move`, role and subrole are **cached at window creation** and refreshed only on minimize / deminimize / native-fullscreen transitions (`event_loop.c`); `--toggle float` with `force=false` returns exit 0 *without doing anything* while the cached `can_move` is false (`window_manager.c:2185-2192`). So ten retries against a cache that never refreshes are ten identical drops, and a longer poll would only burn more `rule --apply` + `arcSync()` passes. And after the give-up, nothing on the machine ever tried again: none of the registered signals toggles float.

The fix moves the retry from "20 s at startup" to "every cheap, user-paced moment for as long as the window floats": `yabai_startup_reconcile.sh float` runs the same `unfloat_pins()` **once** — no `rule --apply`, no `arcSync()`, no `sudo` — from three signals: `space_changed`, `window_deminimized` (the one event that refreshes the cache) and, since 2026-09-15, `window_focused` filtered to the pinned apps (`app=$YABAI_PINNED_APPS_RE`, so focusing anything else forks nothing). The common path (nothing pinned is floating) is one query + one jq and exits in ~40 ms. Properties worth knowing:

- **Routes are decided from the focus state read immediately before each toggle**, not from the pass's snapshot: `direct` when home *is* the focused space on the focused display (the toggle tiles it correctly by itself); `flip` (toggle, then `--layout bsp` → `--layout stack` on home) when home is a background **stack** space *and the active space is also a stack* — on a bsp active space the transient registration would visibly re-split the user's tiles, so that case is skipped and logged rather than repaired; `skip` when home is bsp and not focused. A heal in flight (`yabai_heal.sh`'s lock present — refresh/reorder focus spaces transiently) forces the flip route.
- **Only floaters already on their home space** are candidates in float mode: a pinned app floating elsewhere is either the user's own (`hyper+t` is only allowed off home) or one the next `application_launched` will re-home first. The user's own floats are additionally recorded by `yabai_toggle_float.sh` (`~/Library/Caches/yabai/keep-float/<id>`) so a later `rule --apply` re-home can't turn them into a "repair"; the startup poll purges the markers (a float that survives a yabai restart is never a choice — yabai re-classified from scratch).
- **Backoff memo**: after 3 consecutive dropped un-floats a window is skipped for 10 min, except on a deminimize or a focus of a pinned app's window. A window whose cache never refreshes (never minimized) is the accepted residual — it's an upstream defect. Escape hatches: minimize/deminimize it; `yabai -m window <managed sibling id> --stack <stuck id>` (**sibling first** — the only single command that clears FLOAT *and* joins the home view regardless of the active space, `window_manager.c:1804-1816`); or restart yabai.
- **The state dir is fixed** (`~/Library/Caches/yabai`, `YABAI_STATE_DIR` in `yabai_common.sh`), not `${TMPDIR:-/tmp}`: yabai's signal actions inherit yabai's `TMPDIR` (`/var/folders/…/T/`), skhd's keybind actions have none (verified 2026-08-29), so under `TMPDIR` a marker written by `hyper+t` was invisible to a sweep, and the heal/borders locks single-flighted nothing across the two callers. All of `yabai_heal.sh`, `yabai_float_borders.sh` and the reconcile now lock there.
- **Locks**: the startup poll touches `alive` every pass, so a sweep defers to it only while that is < 30 s old and reaps an orphan otherwise (an orphan used to disable the repair for the whole login session, silently). A second `--restart-service` while a poll from the *previous* yabai is running is no longer dropped: the new poll asks the holder to stop between passes (`stop` file), waits out a whole pass (~8 s), and takes over. Since 2026-10-06 the lock is **owned by pid** (`$LOCK/pid`): a holder releases it — and keeps polling — only while it still owns it, because the displaced poll's EXIT trap used to delete the *new* holder's lock, after which sweeps and the resolver no longer saw a live poll. The focus/deminimize sweeps wait up to 1.5 s for the sweep lock instead of losing it to the `space_changed` sweep that fired a moment earlier. The flip pair runs under `trap '' TERM` so nothing can strand a space in bsp.
- **Stale locks at startup**: a restart kills yabai's whole process group, so any lock older than the running yabai (`pin_resolve`, `heal`, `float_sweep`, `float_borders`, and `yabai_displays.lock`) can't have a live holder; startup mode clears them (`startup cleared-stale-lock …`). A stale `heal.lock` used to force every sweep onto the flip route, and a stale displays lock disabled `yabai_terminal_follow.sh` until the next dock.
- **Log**: `~/Library/Logs/yabai/reconcile.log`, one line per attempt/skip/give-up/exit with the window's pre-state (`cm` can-move, `cr` can-resize, `sr` subrole, `lvl` level, `ax` has-ax-reference) — the three questions this incident couldn't answer (did the repair run? which predicate blocked it? what finally fixed it?) are one `grep id=<wid>` next time. Trimmed to the last 2000 lines at startup once it passes 200 KB. `float --dry-run` prints what a sweep would do (startup mode has no dry run — it would still `rule --apply`, arcSync and wipe markers).
- **Rejected**: an automated `rule --apply app=^X$ manage=on` escalation — per-*app*, sets `WINDOW_RULE_MANAGED` *before* the eligibility check, force-tiles every root window of the app (Electron helpers included) into the **active** view, permanently; an AX-gated launch wrapper (an unsigned AX probe under launchd has no TCC trust, so every restart would wait the full timeout); a `system_woke` trigger (wake alone never misclassifies, and 0.3 s after wake AX is exactly in the stalled state).

Two bugs found while testing this, both pre-dating it and both fixed here:

- **`pins_settled()` counted windows yabai can never move.** An Electron app publishes hidden helper windows with an *empty* subrole and `can-move: false` — Claude Desktop ships two, parked on whatever space it launched from. `rule --apply` cannot move them home, so the poll could never settle and burned its **entire 90 s cap at every login**, re-running `rule --apply` + `arcSync()` every 2 s. The off-home check now only counts `AXStandardWindow` root windows.
- **`yabai -m query --spaces` intermittently returns a bare `[`** (seen 2026-08-20 and again 2026-08-21, persisting for minutes) while `--windows` and the *indexed* form `--spaces --space <sel>` keep working. Anything polling on the bulk form goes blind and spins to its cap. `yabai_common.sh` now has `yabai_spaces_json()`: retry briefly, then rebuild the array one canonical label at a time. Labeled spaces only, which is all any consumer wants. `yabai_workspace_refresh.sh` can't use it (it needs the *unlabeled* spaces too, to reuse them), and until 2026-10-06 it only checked the exit code: a bare `[` read as "zero spaces", so it ran `space --create` once per label, twice per run — 20 spurious spaces. It now validates every bulk query (`type == "array" and length > 0`, 3 tries, else skip this run) and stops creating for the rest of a run once a `space --create` yields no new space.

**Unresolved pinned windows → guarded yabai restart (`yabai_pin_resolve.sh`, added 2026-10-06).** A third failure, the one that *only* a manual `yabai --restart-service` used to fix: Claude Desktop stranded on a random space (`agent`, 2026-10-06 after login; also after its 2026-10-05 stealth update relaunch). The window was **listed** by `query --windows` but with `has-ax-reference: false`, empty subrole, `can-move: false` — and `window <id> --space ai` answered `could not locate the window to act on!`. The yabai source (v7.1.25) explains it: an app's windows on *other* spaces are resolved only by a brute force over AX element ids (`window_manager_add_existing_application_windows`, `refresh_index == -1`) that runs **only at yabai start** (`window_manager_begin`). At login, a window that isn't AX-ready at that instant goes on `applications_to_refresh`, whose later retries (space change, app activation) read only `kAXWindows` — which lists **active-space** windows, and the stuck window is by definition on a space the user isn't looking at. On an app (re)launch — Claude's stealth update relaunch — it's worse: the launch handler reads only `kAXWindows` and never queues a retry at all, so a window that comes up on an inactive space is never tracked (upstream issue #2833, unmerged patch: run the brute force on launch too). So no rule, no float sweep, nothing can reach it; `pins_settled()` even skipped it as a "helper" (empty subrole), so the startup poll declared success in 0 s. Only a restart re-runs the brute force. The resolver automates that restart:

- **Detection** (`YABAI_JQ_PIN_UNRESOLVED` in `yabai_common.sh`): a pinned app's root window with `has-ax-reference: false`, at least 300×200, **and the app has no resolved standard window at all** — so an unresolvable Electron helper next to a healthy main window never triggers a restart. "The app" is matched by **pid**, not name: an unresolved window reports the *process* name, a resolved one the app name, and those differ for WezTerm (`wezterm-gui` vs `WezTerm`).
- **Triggers**: the float sweep's existing single query (every `space_changed` / pinned `window_focused` / `window_deminimized` — zero extra forks unless something is stuck), `application_launched` for pinned apps (label `pin_resolve_launched`, `--watch 30`: the relaunched window appears ~6 s after the event), `system_woke` (label `pin_resolve_woke`, `--delay 3`), and the end of the startup poll (`--delay 5`).
- **Guards**: the same window id must still be unresolved 2 s later; never during a live startup poll; **never behind the lock screen** (right after wake AX is stalled, so the new yabai's discovery pass would fail for *every* other-space window — the first space switch after unlock re-triggers it via the sweep); ≤ 1 restart per 30 s — a check landing inside that gap (typically the startup check right after our own restart) **waits the gap out and looks once more** instead of dropping the retry; ≤ 2 restarts per window id (memo entries expire after a day; an expired count restarts at 0) — then a single `gave-up` log line and it's left alone. The memo is **scoped to the login session** (WindowServer pid): CGWindowIDs restart at every login in a deterministic order, so Claude tends to get the same small id each time and a memo outliving the login would spend this login's budget on last login's window. Single-flighted (mkdir lock). Idle cost: none; a no-op run is ~30 ms.
- **Restore after restart**: a restart silently resets per-space layouts (every space comes back the global `stack`, so a `hyper+fn+b` bsp space reverts) and re-classifies floats (a `hyper+t` float comes back tiled). Right before restarting, the resolver snapshots non-`stack` labeled spaces and the `hyper+t` floats (non-pinned floating standard windows + `keep-float/` markers) to `~/Library/Caches/yabai/restore_after_restart`; the startup poll replays it after it settles, only if < 5 min old, toggling only windows that came back tiled. Hand-tuned bsp split ratios are not recoverable.
- **Log**: `~/Library/Logs/yabai/resolve.log` (`restart` / `defer` / `gave-up` lines with the stuck `id app space`; the replay logs `startup restored-after-restart …` in `reconcile.log`). `yabai_pin_resolve.sh manual --dry-run` prints what it would do.

**Upstream context.** yabai #2833 (the launch path never brute-forces inactive-space windows; patch unmerged as of 2026-10-06) is why the resolver exists for the update-relaunch case — a yabai with that patch would make the `pin_resolve_launched` trigger redundant, not the login one. Separately, on macOS 26.6 the v7.1.25 scripting addition's `add_space` pattern no longer matches: `yabai -m space --create` returns 0 and silently does nothing (verified live 2026-10-06), and the `sa=FAIL` lines in `reconcile.log` were that, not the sudoers pin (which matches the current binary). Fixed on yabai master in `dd84572`; until a release carries it, refresh's create backstop (above) keeps a failed create from looping.

> **Debugging signals:** `yabai -m signal --list` returns every registered signal as JSON (label, event, app filter, action) — `yabai -m signal --list | jq -r '.[] | "\(.label)\t\(.event)"'`. (There is no `query --signals`; it errors `unknown command`.) Unlabeled signals (`dock_did_restart`, the plain `window_created` / `space_changed` / `application_launched` handlers) show an empty label.

#### Environment Variables Exported

| Variable | Value | Consumed By |
|----------|-------|-------------|
| `YABAI_WORKSPACE_REFRESH` | `${HOME}/code/various_scripts/yabai_workspace_refresh.sh` | Startup only (the one `"$YABAI_WORKSPACE_REFRESH" startup` call). The hotplug/reader scripts run the refresh script too, but via their own `$SCRIPT_DIR` path, not this env var. |
| `YABAI_DISPLAYS` | `${HOME}/code/various_scripts/yabai_displays.sh` | `display_added` / `display_removed` signals |
| `YABAI_MOUSE_FOLLOW` | `${HOME}/code/various_scripts/yabai_mouse_follow.sh` | `display_changed` signal |
| `YABAI_HEAL` | `${HOME}/code/various_scripts/yabai_heal.sh` | `space_destroyed` / `mission_control_exit` signals (debounced self-heal) |
| `YABAI_STARTUP_RECONCILE` | `${HOME}/code/various_scripts/yabai_startup_reconcile.sh` | Backgrounded once at yabai startup (login-race + stray-float fix: polls — re-apply rules + Arc re-pin + un-float misclassified pinned windows — until pinned apps are home and tiled, capped by `YABAI_RECONCILE_CAP` ≈90 s); also the action (`… float`) of the 3 float-sweep signals (`float_sweep_space_changed` / `_deminimized` / `_focused`) |
| `YABAI_PIN_RESOLVE` | `${HOME}/code/various_scripts/yabai_pin_resolve.sh` | `pin_resolve_launched` (`application_launched`, pinned apps) / `pin_resolve_woke` (`system_woke`) signals — guarded restart for an unresolved pinned window |
| `YABAI_LAYERS_REFRESH` | `/usr/bin/notifyutil -p com.mackhaymond.layerbar.refresh` | `layers_refresh_focus` / `layers_refresh_destroyed` signals (the `window_created` / `space_changed` handlers post the same notification inline) — instant LayerBar refresh |
| `YABAI_FLOAT_BORDERS` | `${HOME}/code/various_scripts/yabai_float_borders.sh` | `window_created` / `window_destroyed` signals + a startup sync + the hyper+t toggle + `yabai_startup_reconcile.sh` after an un-float — draws a JankyBorders border around floating windows (daemon runs only while something floats) |

### 3.2 Keybinds (skhdrc, fired by Karabiner)

**File:** `~/.config/skhd/skhdrc`

`skhdrc` is the declarative bind list (skhd syntax). **Karabiner executes it, not the skhd daemon:** `chezmoi apply` compiles it into `karabiner.json` via `dot_config/skhd/executable_skhd-to-karabiner.py`, so every bind is matched at the HID level where Secure Input (password fields in Chrome/Arc) can't blind it the way it blinds skhd's event tap. All paths are templated during chezmoi apply; `{{ .chezmoi.homeDir }}` becomes `/Users/mackhaymond`.

> **Keep in sync — hand-mirrored, no auto-generation.** This bind list lives in THREE files: `dot_config/skhd/skhdrc.tmpl` (the **source of truth**), the `HELP_COL1/2/3` tables in `dot_hammerspoon/init.lua` (the on-screen **`hyper+fn+?`** help overlay; `Esc` closes it), and this README (§3.2 tables + §6 cheat sheet). Change a bind in skhd and you MUST update the overlay tables **and** both README sections, or the docs and the on-screen help will lie.

**Hex Key Codes:**
- `0x32` = Backtick (`)
- `0x2A` = Backslash (\)
- `0x21` = Left bracket (`[`)
- `0x1E` = Right bracket (`]`)
- `0x29` = Semicolon (`;`)
- `0x27` = Apostrophe (`'`)
- `0x2C` = Slash (`/`) — i.e. `?` once hyper's shift is applied (the help-overlay bind)

All other keys referenced by character (e.g., `hyper - 1`, `hyper - z`).

#### Focus Workspace (Hyper Layer)

Move focus to a labeled space without moving it — on whichever display the label lives (a label pushed to the external is focused there; the space never comes to you).

| Keybinding | Key | Script | Workspace |
|---|---|---|---|
| `hyper - 0x32` | Backtick | `yabai_workspace.sh focus terminal` | terminal |
| `hyper - 1` | 1 | `yabai_workspace.sh focus main` | main |
| `hyper - 2` | 2 | `yabai_workspace.sh focus school` | school |
| `hyper - tab` | Tab | `yabai_workspace.sh focus todo` | todo |
| `hyper - q` | Q | `yabai_workspace.sh focus schedule` | schedule |
| `hyper - w` | W | `yabai_workspace.sh focus mail` | mail |
| `hyper - e` | E | `yabai_workspace.sh focus calendar` | calendar |
| `hyper - d` | D | `yabai_workspace.sh focus messages` | messages |
| `hyper - f` | F | `yabai_workspace.sh focus ai` | ai |
| `f18` | Caps+Esc | `yabai_workspace.sh focus agent` | agent (no-op when empty) |

#### Send Window to Workspace (Hyper+Fn Layer)

Move the focused window to a target space and **follow focus to it** (unless pinned to home space — then it stays put and focus is unchanged).

| Keybinding | Key | Script | Destination |
|---|---|---|---|
| `hyper + fn - 0x32` | Fn+Backtick | `yabai_send_window.sh terminal` | terminal |
| `hyper + fn - 1` | Fn+1 | `yabai_send_window.sh main` | main |
| `hyper + fn - 2` | Fn+2 | `yabai_send_window.sh school` | school |
| `hyper + fn - tab` | Fn+Tab | `yabai_send_window.sh todo` | todo |
| `hyper + fn - q` | Fn+Q | `yabai_send_window.sh schedule` | schedule |
| `hyper + fn - w` | Fn+W | `yabai_send_window.sh mail` | mail |
| `hyper + fn - e` | Fn+E | `yabai_send_window.sh calendar` | calendar |
| `hyper + fn - d` | Fn+D | `yabai_send_window.sh messages` | messages |
| `hyper + fn - f` | Fn+F | `yabai_send_window.sh ai` | ai |
| `f19` | Fn+Caps+Esc | `yabai_send_window.sh agent` | agent |

**Pinned Apps (cannot be sent off their home spaces):**
- wezterm → terminal
- Todoist → todo
- Granola → schedule
- Spark Mail → mail
- Notion Calendar → calendar
- Messages → messages
- ChatGPT → ai
- Claude → ai
- Conductor, Claudia, OpenChamber, OpenCode, Jean, T3 Code (Alpha), Muse → agent
- Arc → protected on `main`/`school` (any Arc window on either is shielded; rare Little-Arc-on-home included)

#### External Scratch-Work Space (`ext`)

Fling a loose, unpinned window onto the external display's on-demand `ext` scratch space (created on first use), or focus it. See the "External scratch-work space (`ext`)" design note. No-op with one display.

| Keybinding | Key | Script | Action |
|---|---|---|---|
| `hyper + fn - g` | Fn+G | `yabai_send_window_external.sh` | Fling the focused unpinned window to `ext` (create + follow); pinned-home apps and Arc-on-main/school are guarded out. Multiple windows stack; cycle with `hyper+z`/`hyper+x` |
| `hyper - g` | G | `yabai_workspace.sh focus ext` | Focus `ext` (no-op if it doesn't exist) |

#### Window Swap (Hyper+Fn)

Swap the focused window with its neighbor in a direction (bsp).

| Keybinding | Key | Command |
|---|---|---|
| `hyper + fn - j` | Fn+J | `yabai -m window --swap south` |
| `hyper + fn - k` | Fn+K | `yabai -m window --swap north` |
| `hyper + fn - h` | Fn+H | `yabai -m window --swap west` |
| `hyper + fn - l` | Fn+L | `yabai -m window --swap east` |

*(The old `hyper+fn+a` / `hyper+fn+s` — `window --space prev`/`next`, the only un-guarded send-to-space path — and `hyper+fn+x` — `space --mirror x-axis`, redundant with `hyper+z`/`hyper+x` in bsp — were removed.)*

#### Bsp Focus, Resize & Layout (Hyper)

Bare-hyper bsp cluster: directional focus, resize, balance, split-orientation, and rotate. All are inline `yabai` commands (no script). No-ops / harmless in a stack space. Note `hyper - a`, `hyper - s` and `hyper - c` are **reserved for Arc** — no skhdrc bind may use them (see the §6 note).

| Keybinding | Key | Command | Action |
|---|---|---|---|
| `hyper - h` | H | `yabai -m window --focus west` | Focus window to the west (bsp; no-op in stack) |
| `hyper - j` | J | `yabai -m window --focus south` | Focus window to the south |
| `hyper - k` | K | `yabai -m window --focus north` | Focus window to the north |
| `hyper - l` | L | `yabai -m window --focus east` | Focus window to the east |
| `hyper - 0x21` | `[` | `yabai -m window --resize right:-60:0` | Make focused window narrower (bsp) |
| `hyper - 0x1E` | `]` | `yabai -m window --resize right:60:0` | Make focused window wider |
| `hyper - 0x29` | `;` | `yabai -m window --resize bottom:0:-60` | Make focused window shorter |
| `hyper - 0x27` | `'` | `yabai -m window --resize bottom:0:60` | Make focused window taller |
| `hyper - b` | B | `yabai -m space --balance` | Balance splits — equalize all split ratios on the space (bsp) |
| `hyper - v` | V | `yabai -m window --toggle split` | Toggle the focused window's split orientation: horizontal ↔ vertical (bsp) |
| `hyper - n` | N | `yabai -m space --rotate 90` | Rotate the whole tree 90° clockwise (bsp); repeat to cycle 90/180/270/0 |

#### Display & Space Movement

| Keybinding | Key | Script/Command | Action |
|---|---|---|---|
| `hyper - 0x2A` | Backslash | `yabai_space_move.sh push` | Move focused space to other display; follow |
| `hyper - 0` | 0 | `yabai_space_move.sh home-all` | Pull all labeled spaces to laptop |
| `f13` | Hyper+F1 | `yabai_display.sh master` | Focus laptop display |
| `f14` | Hyper+F2 | `yabai_display.sh external` | Focus external display |

#### Stack/Mirror, Maximize & Layout Toggle (Hyper)

`hyper - z` / `hyper - x` are **dual-role / layout-aware** (logic lives in the scripts): in a **stack** space they cycle stack layers; in a **bsp** space (where the stack is meaningless) they mirror the tree instead — `z` = horizontal (`--mirror x-axis`), `x` = vertical (`--mirror y-axis`). Single-laptop stack-cycle behavior is unchanged.

| Keybinding | Key | Script/Command | Action |
|---|---|---|---|
| `hyper - z` | Z | `yabai_skhd_stack_next.sh` | **Stack:** focus next stack layer (wrap to first). **Bsp:** mirror tree horizontally (`space --mirror x-axis`) |
| `hyper - x` | X | `yabai_skhd_stack_prev.sh` | **Stack:** focus previous stack layer (wrap to last). **Bsp:** mirror tree vertically (`space --mirror y-axis`) |
| `hyper - m` | M | `yabai -m window --toggle zoom-fullscreen` | Toggle maximize — zoom the focused window to fill its space |
| `hyper - t` | T | `yabai_toggle_float.sh` | Toggle the focused window's float (works in **both** stack & bsp). **Directional guard:** refuses to *float* a pinned app on its home space (or Arc on main/school), but always allows *un*-floating one — that's the manual way back from a window yabai flagged FLOAT on its own. manage=off apps are not guarded |
| `hyper + fn - m` | Fn+M | `yabai -m window --toggle native-fullscreen` | Toggle native fullscreen (global; **no-op on WezTerm** by design — see the "WezTerm is not fullscreenable" note) |
| `hyper + fn - b` | Fn+B | `yabai_skhd_mode.sh` | Toggle space layout (bsp ↔ stack) |
| `hyper + fn - s` | Fn+S | `display_sleep_lock.sh` | Sleep displays now (+ session locks via the immediate screen-lock policy). On the fn layer: bare `hyper-s` is reserved for Arc (was BetterTouchTool). Must always work, even over an agent-held display-awake assertion — forced sleep overrides idle assertions (cua v2 canaries this) |

#### Native-Fullscreen App Access (Hyper)

Reach apps put into macOS native fullscreen — they live in their own Spaces outside the labeled model, so the focus-workspace keys can't reach them. Ordinal = mission-control order (display, then space index); **WezTerm is excluded** (it's the terminal, reached with `hyper+\``).

| Keybinding | Key | Script | Action |
|---|---|---|---|
| `hyper - 3` … `hyper - 9` | 3–9 | `yabai_fullscreen_focus.sh 1…7` | Focus the 1st…7th native-fullscreen app (no-op if that many aren't open) |

*(`hyper - 1`/`- 2` = focus main/school; `hyper - 0` = pull-home. So 3–9 were free.)*

### 3.3 Karabiner-Elements Key Remapping

**File:** `~/.config/karabiner/karabiner.json` (generated — edit `.chezmoitemplates/karabiner-base.json` or `skhdrc.tmpl`, see §2)

Karabiner handles keyboard input at the HID level: it creates the hyper modifier and, since 2026-09-23, **fires every skhdrc bind itself**. `skhd-to-karabiner.py` prepends one generated rule (one manipulator per bind, fn-layer binds first) to the hand-written base rules.

#### Core Remapping: Caps Lock → Hyper

**From:** `caps_lock` (any modifiers optional)  
**To:** `left_shift + left_command + left_control + left_option` (the "hyper" modifier)

This single remapping enables nearly every downstream binding.

#### Complex Modifications: F-Key Chords

The base config writes these chords as "→ F13/F14/F18/F19" (the names skhdrc binds), but Karabiner never re-processes its own output, so the generator **rewrites each chord to run the matching skhdrc command directly** — no F-key is ever emitted. A bare-key bind with no chord producing it fails `chezmoi apply`.

| From | Base output | skhdrc bind it runs | Purpose |
|------|----|----|---------|
| F1 + hyper | F13 | `f13 : yabai_display.sh master` | Focus master (laptop) display |
| F2 + hyper | F14 | `f14 : yabai_display.sh external` | Focus external display |
| Escape + hyper (no fn) | F18 | `f18 : yabai_workspace.sh focus agent` | Focus agent workspace |
| Escape + hyper + fn | F19 | `f19 : yabai_send_window.sh agent` | Send window to agent workspace |
| Caps_Lock + Escape (simultaneous) | F18 | `f18 : yabai_workspace.sh focus agent` | Alt ergonomic path to focus agent |
| Fn + Caps_Lock + Escape (simultaneous) | F19 | `f19 : yabai_send_window.sh agent` | Alt ergonomic path to send to agent |

#### Arc-only Shortcuts (were BetterTouchTool)

BetterTouchTool is no longer running; its Arc shortcuts live in `karabiner-base.json`, active only while Arc is frontmost: `hyper+a` → `option+command+left_arrow`, `hyper+s` → `option+command+right_arrow`, `hyper+c` → `shift+command+c` (copy URL), plus the double-tap caps_lock / right_shift → `ctrl+tab`. **Trap:** the generated skhdrc rule is *prepended*, so an skhdrc bind on `hyper - a`/`s`/`c` would win over these and silently kill the Arc shortcuts — keep those three keys out of skhdrc.

#### System FN Row Preservation

F1–F4 remain mapped to macOS functions (brightness ×2, Mission Control, Launchpad) to preserve system functionality. F5 is left as a plain F5 (no consumer/media function mapped).

#### Ignored Device

An external **Apple** keyboard (vendor_id 1452 = Apple Inc. / `0x05AC`, product_id 34304 / `0x8600`) is ignored, so Karabiner only processes the built-in keyboard — caps_lock→hyper and the F13/F14/F18/F19 chords therefore fire only on the built-in keyboard, not on this external Apple keyboard. (vendor_id 1452 is Apple, not a third-party mechanical board; confirm the exact model if you need it.)

### 3.4 WezTerm Terminal Configuration

**File:** `~/.config/wezterm/wezterm.lua`

WezTerm is the primary terminal, pinned to the `terminal` space and fully managed by yabai for resizing across displays.

#### Window Integration with Yabai

**Critical setting: `window_decorations = "RESIZE|MACOS_FORCE_SQUARE_CORNERS"`**

- Maintains borderless, edge-to-edge aesthetics.
- Reports window as resizable to macOS, allowing yabai to apply any dimensions.
- Avoids native fullscreen locking that would prevent adaptive cross-display resizing.
- `MACOS_FORCE_SQUARE_CORNERS` removes macOS's rounded window corners. It **requires the OpenGL `front_end`** (below): under WebGPU the window initializes at the wrong scale when this flag is present at startup.

#### Startup & Tmux

**Default program:**
```lua
config.default_prog = { "{{ .homebrew_prefix }}/bin/tmux", "new-session", "-A", "-s", "main" }
```
(The source is a chezmoi template; `{{ .homebrew_prefix }}` renders to `/opt/homebrew` on this machine, i.e. `/opt/homebrew/bin/tmux`.) Every new WezTerm window attaches or creates a tmux session named `main`.

**Startup window state:** WezTerm starts as a **normal (non-fullscreen) window** and stays one — it is intentionally **not fullscreenable**. There is no `gui-startup` fullscreen toggle. yabai's `space=terminal` rule + the `window_created` hook place it on the `terminal` space, which `yabai_reorder_spaces.sh` keeps at canonical **index 1**, where it tiles as the single stack window. `hyper+fn+m` (yabai's native-fullscreen toggle) **no-ops on WezTerm** — see the "WezTerm is not fullscreenable" design note. *(Historically WezTerm auto-fullscreened on startup via a `gui-startup` `toggle_fullscreen()`; that was removed — see git `4e99ec9`.)*

#### Display & UI Settings

- `enable_tab_bar = false` — tabs managed by tmux, not WezTerm.
- `enable_kitty_graphics = true` — inline images/SVG support.
- `scrollback_lines = 0` — scrollback via tmux history, not terminal buffer.
- `native_macos_fullscreen_mode = false` — WezTerm is intentionally **not fullscreenable** (a normal tiled window by design), so `false` drops WezTerm's macOS native-fullscreen capability entirely. Independently, `hyper+fn+m` (yabai's native-fullscreen toggle) **no-ops on WezTerm regardless of this setting**, because the borderless `RESIZE` decoration (no title bar) has no macOS native-fullscreen action — verified on dual-display hardware 2026-06-04 (yabai fullscreens a titled app like Preview, but not WezTerm). The `false` makes the "stays a normal window" intent explicit in config.
- `macos_fullscreen_extend_behind_notch = true` — extends rendering behind notch.

#### Performance

- `front_end = "OpenGL"` — GPU backend. Chosen over WebGpu because `MACOS_FORCE_SQUARE_CORNERS` (square corners) triggers a WebGPU square-corner scaling bug; OpenGL renders them correctly.
- `max_fps = 120` — matches ProMotion external display.
- `animation_fps = 1`, `cursor_blink_rate = 0`, `use_ime = false` — minimal overhead.

#### Keybindings

All WezTerm keybindings forward to tmux prefix (`Ctrl+S`) chords, delegating window/pane management to tmux:

| WezTerm | Sends | Tmux Command | Result |
|---------|-------|------------|--------|
| Cmd+T | Ctrl+S, C | `bind c` | New window |
| Cmd+Shift+T | Ctrl+S, Ctrl+T | `bind C-t` | New window at end (HOME cwd) |
| Cmd+Shift+R | Ctrl+S, Shift+R | `bind R` | Recreate window in place (same cwd) |
| Cmd+W | Ctrl+S, X | `bind x` | Kill pane/window |
| Cmd+Shift+W | Ctrl+S, Ctrl+L | `bind C-l` | Kill pane |
| Cmd+1 through Cmd+9 | Ctrl+S, N | `bind N` | Select window N |

## 4. The Scripts: Purpose & Interconnections

### Script Reference Table

| Script | Arguments | Purpose |
|--------|-----------|---------|
| `yabai_workspace.sh` | `focus <label>` | Focus workspace by label, wherever it lives (never moves it) |
| `yabai_send_window.sh` | `<label>` | Move focused window to space and follow focus to it; blocked (focus unchanged) if window is pinned and already on home space |
| `yabai_display.sh` | `master` \| `external` | Focus the laptop or external display; no-op on single display |
| `yabai_space_move.sh` | `push` \| `home-all` | Cross-display space movement: push focused space to other display (with follow), or pull all labels home |
| `yabai_displays.sh` | `added` \| `removed` | Hotplug handler: dock = refresh cache (non-destructive); undock = pull home safety net |
| `yabai_workspace_refresh.sh` | (none; on-demand) | Reconcile canonical labels on all displays (validated queries; conservative label-follows-app; positional labels skip native-fullscreen Spaces); refresh display topology cache; re-register the pin rules (`yabai_pin_rules_add`) + `rule --apply`; reorder |
| `yabai_heal.sh` | (none; signal handler) | Debounced self-heal — single-flight (mkdir lock) + settle, then `yabai_workspace_refresh.sh`. Bound to `space_destroyed` / `mission_control_exit` |
| `yabai_startup_reconcile.sh` | `[startup]` \| `float [--dry-run]` | **`startup`** (backgrounded from yabairc): clears locks older than this yabai, re-loads the SA (`sudo -n`) + **polls until stable** (re-apply rules + Arc re-pin + un-float misclassified pinned windows, until every running pinned app is home **and tiled**, ~90 s cap), restores bsp layouts + `hyper+t` floats after an automatic restart, then runs `yabai_pin_resolve.sh startup --delay 5`. **`float`**: one un-float pass, run by the 3 float-sweep signals. Single-flighted per mode (pid-owned mkdir locks) |
| `yabai_pin_resolve.sh` | `<event> [--delay S] [--watch S] [--dry-run]` | Guarded `yabai --restart-service` for a pinned window yabai lists but can't act on (2 s confirm, ≤1/30 s, ≤2 per window, never during the startup poll or behind the lock screen); snapshots layouts/floats first. Log `resolve.log` |
| `yabai_send_window_external.sh` | (none) | `hyper+fn+g`: fling the focused unpinned window to the external's on-demand `ext` space (create + follow); no-op with one display |
| `restart-yabai.sh` | (none; Raycast) | `yabai --restart-service` from Raycast; not wired to any signal/bind |
| `yabai_skhd_mode.sh` | (none) | Toggle focused space layout (bsp ↔ stack) |
| `yabai_toggle_float.sh` | (none) | Toggle the focused window's float (`hyper+t`); works in both stack & bsp; **directional guard** — refuses to float a pinned app on its home space + Arc on main/school, always allows un-floating (manage=off apps not guarded); calls `yabai_float_borders.sh sync` after toggling |
| `yabai_float_borders.sh` | `sync` | Reconcile the JankyBorders daemon to the current floating-window set — start it (subtle white, round, width 2) when ≥1 window floats, live-update its app `whitelist`, kill it when none. **Single-flight (mkdir lock + 0.15 s settle, mirrors `yabai_heal.sh`)** so concurrent syncs can't spawn duplicate daemons (`borders` is not a process-level singleton); a daemon count ≠ 1 is self-healed to one. Wired to `window_created`/`window_destroyed` + startup + the hyper+t toggle + `yabai_startup_reconcile.sh` after an un-float. No-op if `borders` isn't installed |
| `yabai_skhd_stack_next.sh` | (none) | **Layout-aware (`hyper+z`):** STACK space → focus next stack layer (wrap to first); BSP space → mirror tree horizontally (`space --mirror x-axis`) |
| `yabai_skhd_stack_prev.sh` | (none) | **Layout-aware (`hyper+x`):** STACK space → focus previous stack layer (wrap to last); BSP space → mirror tree vertically (`space --mirror y-axis`) |
| `yabai_mouse_follow.sh` | (none; signal handler) | Warp mouse cursor to focused display center (if not already there) |
| `yabai_screen_flash.sh` | (none) | **DISABLED (dormant)** — was the external-display focus border flash; signal removed 2026-06-04 |
| `yabai_reorder_spaces.sh` | (none) | Slide labeled spaces into canonical order per display (reserves non-master's first space as scratch); handles fullscreen spaces; preserves the focused space across the moves |
| `yabai_fullscreen_focus.sh` | `<ordinal>` | Focus the Nth native-fullscreen app in mission-control order (`hyper+3-9`); excludes WezTerm |
| `yabai_terminal_follow.sh` | (none; space_changed hook) | Re-pin `terminal` label onto WezTerm's space (incl. fullscreen) + reorder; sweep surplus empty husk spaces |

### How It All Connects

#### Data Flow: Cache-Driven Architecture

```
yabai_workspace_refresh.sh (cache writer)
    ├── Queries: yabai -m query --displays / --spaces
    ├── Resolves: MASTER_DISPLAY_UUID match → MASTER_DISPLAY_INDEX
    ├── Fallback: smallest-area display (laptop)
    ├── Writes atomically: ~/.cache/yabai/workspace_cache.env
    └── Contents: DISPLAY_COUNT, MASTER_DISPLAY_INDEX, EXTERNAL_DISPLAY_INDEX, MASTER_DISPLAY_UUID

Readers (scripts that `. "$CACHE_FILE"` to resolve topology):
    ├── yabai_workspace.sh      (focus)
    ├── yabai_display.sh        (master/external focus)
    ├── yabai_space_move.sh     (push/home-all)
    ├── yabai_displays.sh       (hotplug; also re-writes it)
    ├── yabai_send_window_external.sh (fling to `ext`)
    └── (yabai_screen_flash.sh was a cache reader too, but the flash is now disabled/dormant)

    Load pattern:
        [ -r "$CACHE_FILE" ] && . "$CACHE_FILE"
        [ -n "$DISPLAY_COUNT" ] || call yabai_workspace_refresh.sh && retry
```

Not cache readers: `yabai_send_window.sh`, `yabai_reorder_spaces.sh`, and `yabai_fullscreen_focus.sh` work purely off live `yabai -m query` (label/UUID lookups), so they need no topology cache.

**Key insight:** The reader scripts source the cache to avoid expensive repeated `yabai -m query --displays` calls. If the cache is missing or stale, they invoke `yabai_workspace_refresh.sh` to heal it.

#### Labeled Space Stability

**Core principle:** Spaces are identified by **labels** (terminal, main, school, etc.), not array indices.

When yabai queries spaces, it uses label-based lookups:
```bash
# Query a space by label (returns live index, even after Mission Control renumbering)
yabai -m query --spaces --space terminal

# Move a space by label (works regardless of current index)
yabai -m space terminal --display "$target_idx"

# Focus a space by label (survives space reordering)
yabai -m space --focus main
```

**Why?** macOS renumbers space indices whenever:
- User opens/closes Mission Control
- Displays plug/unplug
- Workspaces are created/destroyed

Labels persist through all these events, making the entire system stable and predictable.

#### Label Repair (`yabai_workspace_refresh.sh`)

When a label *is* lost (a destroyed/merged space, a yabai restart), refresh puts it back in two ways:

- **Positional:** a missing label goes to its canonical position N = the **Nth regular space on the master**; native-fullscreen Spaces are not counted and never handed a label (counting them used to shift every later label by one). If that slot is taken it falls back to the first unlabeled regular space, and only then creates one.
- **Label-follows-app:** each pinned app's label then moves onto the space where that app actually lives — the repair for labels handed out by position. Since 2026-10-06 this is **conservative**, because following *any* window of the app let a stray one drag the label along (a Claude window stranded on `todo` at a restart took `ai` there; Claude in native fullscreen pulled `ai` onto the fullscreen Space):
  1. a label **stays** where it is if that space already hosts one of its apps;
  2. it never takes a space whose **own** label's app lives there (that window is the stray, not the label);
  3. it never lands on a **native-fullscreen** Space (`terminal` excepted — `yabai_terminal_follow.sh` deliberately puts it on a fullscreen WezTerm).

  `ai` is one call for `^(ChatGPT|Claude)$`, so rule 1 keeps it wherever either app is (two calls flipped it to whichever ran second).

After relabeling, refresh re-registers the pin rules (see the `space=` caveat in §3.1) and runs `rule --apply`, then the reorder.

#### Dock/Undock Flow

**On Plug (External Monitor Connected):**
1. macOS emits `display_added` signal.
2. yabai's signal handler calls `yabai_displays.sh added`.
3. Script acquires lock (coalesce duplicate signals).
4. Polls display count until stable.
5. Calls `yabai_workspace_refresh.sh`:
   - Queries new display topology.
   - Resolves MASTER_DISPLAY_INDEX and new EXTERNAL_DISPLAY_INDEX.
   - Ensures all 10 canonical labels exist on master.
   - **Does NOT move any spaces** (external comes up empty-and-ready).
   - Writes cache.
6. User manually pushes workspaces via `hyper+\` (Karabiner → yabai_space_move.sh push) or pulls master workspaces to external display.

**On Unplug (External Monitor Disconnected):**
1. macOS emits `display_removed` signal.
2. yabai's signal handler calls `yabai_displays.sh removed`.
3. Script acquires lock and settles display count.
4. Calls `yabai_workspace_refresh.sh` (refresh cache; see topology change).
5. **Pull-home safety net:**
   - For each of the 10 canonical labels:
     - If label lives on non-master display, move it: `yabai -m space <label> --display <master>`
   - Resolves master by UUID, then area, then cache, then default 1.
6. Applies rules and refreshes again (final settle).
7. Result: all labeled workspaces are back on the laptop, ready for the next dock.

**Why non-destructive on dock, destructive on undock?**
- **Dock:** External is transient; keep laptop layout untouched; external comes up clean for fresh work.
- **Undock:** Prevent orphaned windows on non-existent display; safety net pulls everything home.

#### Mouse Follow (and the disabled Screen Flash)

**When cross-display focus changes** (via F13, F14, or any focus binding that jumps displays), yabai's `display_changed` signal fires and runs:

- **`yabai_mouse_follow.sh`** — queries the focused display and the display under the cursor; if they differ, warps the cursor to the focused window/display center. Single-display guard: no-op on a laptop with no external.

> **Screen flash — DISABLED (2026-06-04, user request).** A second `display_changed` handler used to flash an orange border on the external display when focus jumped there. That signal was removed from `yabairc`; the helper `yabai_screen_flash.sh` / `.js` and their `YABAI_FLASH_*` tunables remain in-tree but **dormant**. To re-enable, restore the `YABAI_SCREEN_FLASH` env var and a `display_changed` signal (`label=flash_external_display`) calling the helper.
>
> *(Preserved gotcha for if it's ever revived: in `yabai_screen_flash.js`, build the border `CGColor` with `$.CGColorCreateGenericRGB(r,g,b,a)` directly — converting a dynamically-created `NSColor` to `.CGColor` through the JXA bridge **SIGKILLs (137)** the process. And a GUI overlay from `osascript` only persists in the Aqua session, so it can only be tested live.)*

#### Terminal Space (not reserved)

The terminal space is WezTerm's home, but it is **not** reserved — other windows may land on it and stay. The `window_created` signal only:

1. Ensures a *new normal* WezTerm window lands on the terminal space (compensates for the racy `space=terminal` rule; a fullscreen WezTerm would be left alone — a defensive guard, though WezTerm isn't fullscreenable).
2. Re-pins the Arc main windows via Hammerspoon on any new Arc window.

It does **not** bounce other apps off the terminal space. (Earlier this enforced "purity" by relocating any non-WezTerm window to `main`; that bounce was removed — windows are free to share the terminal space with WezTerm.)

#### Canonical Space Ordering

The 10 spaces are always maintained in the order: terminal, main, school, todo, schedule, mail, calendar, messages, ai, agent.

**Responsibility:** `yabai_reorder_spaces.sh` (called at the end of `yabai_workspace_refresh.sh` and after `yabai_space_move.sh` operations).

**Per-display logic:**
- **Master (laptop):** Labels start at the first space (index 1, 2, 3, ...).
- **External:** First space reserved as scratch (macOS destroys it on disconnect). Labels start at the second space.

**Algorithm:**
1. For each display, find minimum space index (`lo`).
2. Set `pos = lo` for master, `pos = lo + 1` for external.
3. For each label in canonical order:
   - If on this display and index ≠ `pos`, move it: `yabai -m space <label> --move <pos>`.
   - Increment `pos`.

**Result:** Wherever labels roam, they maintain their stable sequence, making the layout predictable and recoverable.

**Focus preservation (why the reorder snapshots the focused space):** a *burst* of `space --move` calls can make macOS yank the **active desktop** onto an unrelated space as a side effect — a yabai/macOS quirk that only surfaces under rapid moves combined with concurrent `yabai -m query` load (i.e. the normal state when the signal handlers are all firing). A single move is silent; the burst is not. Because the reorder fires after a `space_changed` (via `yabai_terminal_follow.sh`) and from the self-heal (`space_destroyed` / `mission_control_exit` → `yabai_workspace_refresh.sh`), the symptom was: press `hyper+<label>`, then the view jumps across a few spaces on its own and lands on the wrong one. (Confirmed by isolation that `space --create` and `space --destroy` of a *non-focused* space are both silent, so the husk-sweep was **not** the cause.) Reordering must never change which space is focused, so `yabai_reorder_spaces.sh`:

1. Snapshots the focused space's **stable id** on entry (`yabai -m query --spaces --space | jq .id`) and arms an `any_moved` flag.
2. After the move loop, **only if a move actually drifted focus**, re-focuses the original space — resolved by **id, not index**, since the moves renumbered indices.

This is a no-op in the common already-ordered case (no moves) and when focus held, so the cheap query-only fast path is byte-unchanged. It fixes all reorder callers at once (`terminal_follow`, `workspace_refresh`, `space_move`). It guarantees the view **settles** on the right space; an occasional brief mid-flight flash during the moves is a yabai/macOS internal that can't be suppressed from here. *(Fix: git `957e9ed`, 2026-06-07.)*

## 5. Common Workflows & Runbook

### Prerequisites & Bootstrap

The single most fragile dependency in the whole system is the **scripting addition**, which yabai needs for native-fullscreen, space create/destroy, and the husk sweep. yabairc loads it on every (re)start via `sudo -n yabai --load-sa` (top of the file) and re-loads it from the `dock_did_restart` signal. For that `sudo` to run **non-interactively from a config/signal with no TTY**, these must be in place — all currently satisfied on this machine, but required to reproduce on a new one:

1. **Partially-disabled SIP.** `csrutil status` must show a *Custom Configuration* with at least **Filesystem Protections: disabled** (set from Recovery with `csrutil enable --without fs` or equivalent). Full SIP blocks the scripting addition.
2. **Scripting addition.** yabai v7 has no separate install step: `sudo yabai --load-sa` installs *and* loads it (the only SA options are `--load-sa` and `--uninstall-sa`), so the yabairc line covers it as long as #3 holds.
3. **Passwordless sudoers entry**, hash-pinned to the yabai binary, at `/etc/sudoers.d/yabai` (mode `0440`, owned by root). Generate the line with:
   ```bash
   echo "$(whoami) ALL=(root) NOPASSWD: sha256:$(shasum -a 256 $(which yabai) | cut -d' ' -f1) $(which yabai) --load-sa"
   ```
   (there is no `--check-sa`). **Regenerate it after EVERY binary change** (upgrade, reinstall, self-build) — a new hash silently invalidates the line; with `sudo -n` the load then fails fast (`sa=FAIL` in `reconcile.log`) and SA-dependent features stop working with no error in the config. Check without running it: `sudo -n -l $(which yabai) --load-sa`.
4. **Accessibility grant follows the signature.** Release binaries are signed with upstream's self-signed `yabai-cert`; a self-built (`brew install --HEAD`) binary is ad-hoc signed. macOS ties the Accessibility (TCC) grant to the signature, so switching between the two needs Accessibility re-granted to yabai — on top of the sudoers line.

   **Installed now: `HEAD-dd84572`** (built 2026-10-06 via `brew install --HEAD koekeishiya/formulae/yabai`; the `7.1.25` keg is kept for rollback). Reason: on macOS 26.6 the 7.1.25 scripting addition (payload 2.1.29) could not resolve `add_space` — its handshake reported capabilities `0x7b` of `0x7f` — so `--load-sa` exited 1 (`sa=FAIL` in reconcile.log) and `space --create` returned 0 while doing nothing. HEAD's payload 2.1.30 reports `0x7f`, and startup logs `sa=ok`. **Switch procedure** (to HEAD, or back to a release once 7.1.26 ships — `brew unlink yabai` then `brew install [--HEAD] koekeishiya/formulae/yabai`, or `brew link` the kept keg): regenerate the sudoers line (one Touch ID), `sudo -n yabai --load-sa`, then in System Settings → Privacy & Security → Accessibility remove `yabai` with − and re-add `/opt/homebrew/bin/yabai` with + (toggling the old entry is not enough), then `yabai --restart-service` — launchd stops retrying after the denied launches, so it will not come back on its own. Check the payload with a read-only handshake on `/tmp/yabai-sa_$USER.socket` (send `01 00 01`; reply = version string, NUL, little-endian u32 capability mask).

**Symptom of a broken SA layer:** yabai starts and tiling/focus all work, but native-fullscreen toggling, space create/destroy, or the WezTerm husk sweep silently no-op. Fix = regenerate the sudoers hash and re-run `sudo yabai --load-sa`, not the config. (Exception: on macOS 26.6 with yabai 7.1.25, `space --create` no-ops however sudoers is set up — an upstream SA bug, see "Upstream context" in §3.1.)

Other login-time dependencies: **Karabiner** (caps_lock→hyper, the F13/F14/F18/F19 chords) and **Hammerspoon** (`hs.autoLaunch(true)`, for Arc pinning — degrades gracefully if absent). `yabai` runs as a user LaunchAgent. The `skhd` daemon is retired (2026-09-23: `launchctl disable gui/501/com.koekeishiya.skhd`; Karabiner fires the skhdrc binds — see §3.2/§3.3); re-enabling it would be harmless but pointless, since Karabiner consumes those keys first.

### Focus a Workspace

**Goal:** Switch focus to a labeled workspace without moving it.

**Action:**
```bash
# From anywhere, press hyper+<key> for the workspace
hyper - 1              # Focus "main" workspace
hyper - 0x32 (`)       # Focus "terminal" workspace
f18                    # Focus "agent" workspace (no-op if the space is empty)
```

**What happens:**
1. Karabiner matches the keybinding (its compiled skhdrc rule).
2. Karabiner runs `yabai_workspace.sh focus <label>`.
3. Script loads display cache (single-display fast-path or multi-display topology).
4. Script queries space's live index by label: `yabai -m query --spaces --space <label>`.
5. Script focuses that index: `yabai -m space --focus <index>`.
6. Focus changes to the space (wherever it lives—laptop or external).
7. If the space is on the external display, `display_changed` signal fires → mouse follows. (The border flash that used to also fire is disabled.)

### Send a Window to a Workspace

**Goal:** Move the focused window to a target workspace **and follow it there** — you land on the target space alongside the window.

**Action:**
```bash
# From anywhere, press hyper+fn+<key> for the destination
hyper + fn - 1         # Send focused window to "main" and follow
hyper + fn - 0x32 (`)  # Send to "terminal" (if not pinned) and follow
f19                    # Send to "agent" and follow
```

**What happens:**
1. Karabiner matches the keybinding (its compiled skhdrc rule).
2. Karabiner runs `yabai_send_window.sh <label>`.
3. Script checks if the window is pinned to a home space (wezterm → terminal, Todoist → todo, etc.).
4. If pinned and already on home space, script exits (bound window cannot move; **focus stays put** — no jump to an empty space).
5. Otherwise, script queries target space's index by label.
6. Script moves window: `yabai -m window --space <label>`.
7. Script follows focus to the moved window (`yabai -m window <id> --focus`), so you end up on the target space. (Focus only follows when the window actually moves.)

### Push a Workspace to the External Display

**Goal:** Move the focused workspace (and all its windows) to the other display.

**Prerequisites:** External display is connected.

**Action:**
```bash
# Press the push binding
hyper - 0x2A (\)       # Push focused workspace to other display
```

**What happens:**
1. Karabiner matches the keybinding (its compiled skhdrc rule).
2. Karabiner runs `yabai_space_move.sh push`.
3. Script loads cache; resolves MASTER_DISPLAY_INDEX and EXTERNAL_DISPLAY_INDEX.
4. Script queries focused space (snapshot id, index, display).
5. Script determines target: if on master → external; if on external → master.
6. Script moves the space: `yabai -m space <index> --display <target>`.
7. yabai resizes all windows in the space to fill the new display.
8. Script follows: resolves the space's new index (indices renumber after move), focuses it (with retry on race condition).
9. Script reorders spaces to restore canonical order.
10. Result: Workspace and all windows are now on the other display, with focus following.

### Pull All Workspaces Home to Laptop

**Goal:** Move all labeled workspaces from the external display back to the laptop (manual alternative to undock safety net).

**Prerequisites:** External display is connected.

**Action:**
```bash
# Press the home-all binding
hyper - 0              # Pull all labeled workspaces to laptop
```

**What happens:**
1. Karabiner runs `yabai_space_move.sh home-all`.
2. Script loads cache.
3. For each of the 10 canonical labels:
   - Script queries the space's current display.
   - If on external, moves it: `yabai -m space <label> --display <master>`.
4. Script focuses master display.
5. Script reorders spaces to restore canonical order.
6. Result: All workspaces are back on the laptop.

### Focus the External Display

**Goal:** Shift active display focus (and mouse) to the external monitor.

**Prerequisites:** External display is connected.

**Action:**
```bash
# Press hyper+F2 (the chord skhdrc calls f14)
f14                    # Focus external display
```

**What happens:**
1. Karabiner matches hyper+F2 and runs the `f14` bind, `yabai_display.sh external`.
2. Script loads cache; resolves EXTERNAL_DISPLAY_INDEX.
3. Script focuses the display: `yabai -m display --focus <external_idx>`.
4. yabai's `display_changed` signal fires.
5. `yabai_mouse_follow.sh` warps cursor to the external display's center.
6. Result: Focus is now on the external display. *(The border flash that used to fire here is disabled.)*

### What Happens on Dock (External Monitor Plug)

1. **macOS hotplug event** → yabai `display_added` signal.
2. **yabai_displays.sh added**:
   - Acquires lock (coalesce 2–3 duplicate signals).
   - Polls display count until stable.
   - Calls `yabai_workspace_refresh.sh`:
     - Queries new topology (DISPLAY_COUNT=2, resolves EXTERNAL_DISPLAY_INDEX).
     - Ensures all 10 labels exist on master.
     - **Does NOT move any workspaces.**
     - Writes cache.
   - Applies rules.
3. **User action:** Manually push workspaces to external (hyper+\) or leave them on laptop.
4. **Result:** External display comes up empty-and-ready; user fills it on demand.

### What Happens on Undock (External Monitor Unplug)

1. **macOS hotplug event** → yabai `display_removed` signal.
2. **yabai_displays.sh removed**:
   - Acquires lock.
   - Polls display count until stable.
   - Calls `yabai_workspace_refresh.sh` (refresh cache; topology is now single-display).
   - **Pull-home safety net:** For each of the 10 labels:
     - If on non-master display, move it home: `yabai -m space <label> --display <master>`.
   - Applies rules; refreshes again (final settle).
3. **Result:** All workspaces are safely back on the laptop.

### Safely Edit Configuration: Complete Runbook

#### Step 1: Edit the Source File

**Location:** `/Users/mackhaymond/.local/share/chezmoi/` (never edit deployed configs directly).

**Example: Add a keybinding to skhd**
```bash
nano /Users/mackhaymond/.local/share/chezmoi/dot_config/skhd/skhdrc.tmpl

# Add a line like (hyper - p is unused; hyper - n is now a live rotate bind):
# hyper - p : /Users/mackhaymond/code/various_scripts/my_script.sh

# (Use {{ .chezmoi.homeDir }} for templates; yabairc uses $HOME at runtime)
```

**Example: Modify yabai rules or signals**
```bash
nano /Users/mackhaymond/.local/share/chezmoi/dot_config/yabai/executable_yabairc
```

**Example: Change Karabiner key mapping**
```bash
nano /Users/mackhaymond/.local/share/chezmoi/.chezmoitemplates/karabiner-base.json
# Be careful with JSON syntax! (karabiner.json itself is generated by
# dot_config/private_karabiner/modify_private_karabiner.json.tmpl = this base + the compiled skhdrc binds)
```

**Example: Add a new shell script**
```bash
# Create the source with executable_ prefix
nano /Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_my_new_script.sh

# Add #!/bin/bash at top; chezmoi will set +x on deployment
```

#### Step 2: Preview Changes

```bash
# Review all pending diffs
chezmoi diff

# Or dry-run the full apply
chezmoi apply --dry-run

# Or review a specific file
chezmoi diff ~/.config/skhd/skhdrc
```

#### Step 3: Apply Changes to Home Directory

```bash
chezmoi apply
```

**What happens:**
1. Chezmoi renders all `.tmpl` files (substitutes `{{ .chezmoi.homeDir }}`, `{{ .homebrew_prefix }}`, etc.).
2. Converts `dot_` prefixes to `.`.
3. Sets `executable_` files to mode +x.
4. Deploys to target locations (~/.config/yabai/yabairc, ~/.config/skhd/skhdrc, ~/code/various_scripts/, etc.).
5. Preserves file permissions and attributes.

#### Step 4: Reload the Service(s)

**For keybinds:** nothing — `chezmoi apply` compiles `skhdrc` into `karabiner.json` and Karabiner hot-reloads it. Karabiner (not the skhd daemon) fires every bind, because skhd's event tap is blind while any app holds Secure Input (see the `skhdrc` header). Check the reload in `~/.local/share/karabiner/log/core_service.log` ("core_configuration is updated").

**For yabai (if changes affect signal handlers, rules, or layout):**
```bash
# Soft restart (keeps windows, reloads config)
yabai --restart-service

# Or hard restart (if soft fails)
launchctl kickstart -k gui/$(id -u)/com.asmvik.yabai
```

> On restart, yabairc re-runs `sudo -n yabai --load-sa` (the scripting addition). This depends on the passwordless-sudo entry described in **Prerequisites & Bootstrap**. If a restart *appears* to succeed but scripting-addition features (native fullscreen, space create/destroy, the husk sweep) quietly stop working, check the scripting addition and the sudoers entry — not the config diff.

**For Karabiner:**
- Auto-reloads (watch the Karabiner menu for confirmation).
- Manual reload: Preferences → Reload JSON

**For WezTerm:**
- Config is watched; changes apply on next tab/window open.
- Or close all WezTerm windows and reopen.

#### Step 5: Commit & Push

```bash
cd /Users/mackhaymond/.local/share/chezmoi

# Stage the modified source file(s)
git add dot_config/skhd/skhdrc.tmpl

# Commit (pre-commit hook runs gitleaks to detect secrets)
git commit -m "Update skhd keybindings: add hyper-p binding for..."

# If gitleaks blocks (found hardcoded secrets):
# 1. Remove the secret from the file
# 2. Re-stage: git add <file>
# 3. Recommit: git commit -m "..."

# Push to remote
git push origin main
```

**Pre-commit Hook Behavior:**
- Runs gitleaks to scan staged files for hardcoded secrets (API keys, AWS creds, etc.).
- **Blocks commit if secrets are found** (must remove them first).
- Redacts secrets in error output for safety.

## 6. Complete Keybinding Cheat Sheet

> **Hand-mirrored — keep in sync.** These binds also live in `dot_config/skhd/skhdrc.tmpl` (source of truth) and the `HELP_COL` tables in `dot_hammerspoon/init.lua` (the `hyper+fn+?` on-screen overlay). Change one, change all three (see the §3.2 banner).

| Keybinding | Physical Key | Action | Notes |
|---|---|---|---|
| **Focus Workspace** |
| `hyper - 0x32` | Backtick | Focus terminal | |
| `hyper - 1` | 1 | Focus main | |
| `hyper - 2` | 2 | Focus school | |
| `hyper - tab` | Tab | Focus todo | |
| `hyper - q` | Q | Focus schedule | |
| `hyper - w` | W | Focus mail | |
| `hyper - e` | E | Focus calendar | |
| `hyper - d` | D | Focus messages | |
| `hyper - f` | F | Focus ai | |
| `f18` | Caps Lock+Escape | Focus agent | Karabiner-mapped; no-op when no agent app is open |
| **Send Window to Workspace** |
| `hyper + fn - 0x32` | Fn+Backtick | Send to terminal | Respects pinned homes |
| `hyper + fn - 1` | Fn+1 | Send to main | Respects pinned homes |
| `hyper + fn - 2` | Fn+2 | Send to school | Respects pinned homes |
| `hyper + fn - tab` | Fn+Tab | Send to todo | Respects pinned homes |
| `hyper + fn - q` | Fn+Q | Send to schedule | Respects pinned homes |
| `hyper + fn - w` | Fn+W | Send to mail | Respects pinned homes |
| `hyper + fn - e` | Fn+E | Send to calendar | Respects pinned homes |
| `hyper + fn - d` | Fn+D | Send to messages | Respects pinned homes |
| `hyper + fn - f` | Fn+F | Send to ai | Respects pinned homes |
| `f19` | Fn+Caps Lock+Escape | Send to agent | Karabiner-mapped |
| `hyper + fn - g` | Fn+G | Fling window to external `ext` space | On-demand; stacks; dissolves to main on hyper+0 / push / undock |
| `hyper - g` | G | Focus external `ext` space | No-op if `ext` doesn't exist |
| **Window Layout & Navigation** |
| `hyper - z` | Z | Stack: next layer / Bsp: mirror horizontal | Stack-cycle wraps to first; bsp = `space --mirror x-axis` |
| `hyper - x` | X | Stack: previous layer / Bsp: mirror vertical | Stack-cycle wraps to last; bsp = `space --mirror y-axis` |
| `hyper + fn - j` | Fn+J | Swap focused window south | bsp |
| `hyper + fn - k` | Fn+K | Swap focused window north | bsp |
| `hyper + fn - h` | Fn+H | Swap focused window west | bsp |
| `hyper + fn - l` | Fn+L | Swap focused window east | bsp |
| `hyper - h` | H | Focus window west | bsp; no-op in stack |
| `hyper - j` | J | Focus window south | bsp; no-op in stack |
| `hyper - k` | K | Focus window north | bsp; no-op in stack |
| `hyper - l` | L | Focus window east | bsp; no-op in stack |
| `hyper - 0x21` | `[` | Resize focused window narrower | bsp; `--resize right:-60:0` |
| `hyper - 0x1E` | `]` | Resize focused window wider | bsp; `--resize right:60:0` |
| `hyper - 0x29` | `;` | Resize focused window shorter | bsp; `--resize bottom:0:-60` |
| `hyper - 0x27` | `'` | Resize focused window taller | bsp; `--resize bottom:0:60` |
| `hyper - b` | B | Balance splits | bsp; `space --balance` |
| `hyper - v` | V | Toggle split orientation (h ↔ v) | bsp; `window --toggle split` |
| `hyper - n` | N | Rotate tree 90° clockwise | bsp; `space --rotate 90` (repeat cycles 90/180/270/0) |
| `hyper - m` | M | Toggle maximize (zoom-fullscreen) | `window --toggle zoom-fullscreen` |
| `hyper - t` | T | Toggle window float | Both stack & bsp; `yabai_toggle_float.sh` — refuses to *float* pinned-on-home + Arc on main/school; *un*-floating always allowed |
| `hyper + fn - m` | Fn+M | Toggle native fullscreen | Global; no-op on WezTerm by design |
| `hyper + fn - b` | Fn+B | Toggle space layout (bsp ↔ stack) | |
| `hyper - a` / `hyper - s` / `hyper - c` | A / S / C | *(reserved for Arc)* | Arc-frontmost-only rules in `karabiner-base.json` (were BetterTouchTool): `opt+cmd+←` / `opt+cmd+→` / `shift+cmd+c` copy URL. Never assign in skhdrc — the generated rule is prepended and would override them |
| `hyper + fn - s` | Fn+S | Sleep displays + lock | `display_sleep_lock.sh`; forced sleep overrides agent display-awake assertions |
| **Display & Cross-Display Movement** |
| `f13` | Hyper+F1 | Focus master (laptop) display | Karabiner-mapped |
| `f14` | Hyper+F2 | Focus external display | Karabiner-mapped |
| `hyper - 0x2A` | Backslash | Push focused space to other display | Moves all windows; follows |
| `hyper - 0` | 0 | Pull all workspaces home to laptop | Safety net for undock |
| **Native-Fullscreen App Access** |
| `hyper - 3` | 3 | Focus 1st native-fullscreen app | `yabai_fullscreen_focus.sh 1` |
| `hyper - 4` | 4 | Focus 2nd native-fullscreen app | ordinal 2 |
| `hyper - 5` … `hyper - 9` | 5–9 | Focus 3rd … 7th native-fullscreen app | ordinals 3–7; no-op if absent |
| **System / Help** |
| `hyper + fn - 0x2C` | Fn+/ (`?`) | Toggle the on-screen keybind help overlay | Hammerspoon `yabaiHelpToggle` (`init.lua`). On the fn layer for a historical reason: under the skhd daemon, bare `hyper+/` was swallowed by macOS's reserved `cmd+?` before skhd's event tap; Karabiner matches below that, so it may no longer apply |
| `esc` | Escape | Close the help overlay | Active only while the overlay is showing |

## Notes & Key Design Decisions

- **Single-laptop-first:** All 10 canonical workspaces live on the master (laptop) display by default. External display is optional and transient.
- **Label-based stability:** Spaces are identified by labels, not indices, surviving Mission Control renumbering and display hotplug.
- **Non-destructive dock:** External monitor comes up empty; user manually pushes workspaces.
- **Pull-home on undock:** Automatic safety net prevents orphaned windows on non-existent displays.
- **Terminal space (not reserved):** WezTerm's home space, but other windows may land on it and stay — the old non-WezTerm bounce was removed. WezTerm itself is still nudged onto it on launch.
- **Pinned apps:** Certain apps (Todoist, Granola, Spark Mail, etc.) are sticky: `hyper+fn+<space>` / `hyper+fn+g` won't send them off their home spaces, and the rules re-home them on every app launch. The one way off is **`hyper+fn+m` native fullscreen**, which moves the window onto its own fullscreen Space (reach it with `hyper+3-9`). Refresh no longer lets the app's label follow it there — only `terminal` may sit on a fullscreen Space.
- **Stack layout:** Only one window visible at a time; navigate with hyper+z/x to cycle through stacked layers.
- **Mouse follow:** Cursor automatically warps to newly focused display (reduced need for manual positioning).
- **Screen flash:** *(disabled 2026-06-04)* — formerly an orange border confirming focus jumped to the external display; the helper remains dormant in-tree.
- **Native-fullscreen access:** Apps put into macOS native fullscreen (non-pinned apps or the browser) live in their own Spaces *outside* the labeled model, so the `hyper+<label>` keys can't reach them. `hyper+3`…`hyper+9` focus the 1st…7th fullscreen app in mission-control order (display, then index) via `yabai_fullscreen_focus.sh`. Mapping is dynamic by position, not pinned per-app. **WezTerm is excluded** from these ordinals (it's the `terminal` workspace, reached with `hyper+\``, even when fullscreen). Note: yabai *can* label, `--move` (reorder), focus, and move fullscreen Spaces between displays — they are not as locked-down as commonly assumed.
- **WezTerm is not fullscreenable (by design):** WezTerm lives as a **normal window** on the `terminal` space (canonical index 1), tiled as the single stack window, and is intentionally never fullscreened. `hyper+fn+m` (yabai's `--toggle native-fullscreen`) **no-ops on WezTerm** — confirmed on hardware 2026-06-04 — because its borderless `RESIZE` decoration (no title bar) has no macOS native-fullscreen action; and `native_macos_fullscreen_mode = false` removes the capability outright so its own toggle can't make a fullscreen Space either. (yabai *can* still fullscreen titled apps like Preview; the `hyper+fn+m` bind is global and works for those.)
  - **`yabai_terminal_follow.sh` (the `space_changed` hook) is therefore mostly dormant** but retained because it still does real cross-display work: it keeps the `terminal` label pinned to WezTerm wherever WezTerm's space goes (e.g. when you `hyper+\` **push** the terminal space onto the external — verified 2026-06-04 the label follows), and reorders. It is a cheap no-op on ordinary same-space switches. The `window_created` fullscreen guard and the husk-sweep are kept as defensive/general machinery (they'd handle a fullscreen Space if one ever appeared, e.g. another app's), but WezTerm itself no longer produces fullscreen husks.
  - **Husk sweep** (general): when the hook relabels, it destroys surplus empty, unlabeled, non-fullscreen spaces, keeping exactly **one empty per display** (`group_by(.display)`; destroyed high-index-first so yabai's index compaction on `--destroy` can't stale a later target; never touches labeled/fullscreen Spaces). Verified on real 2-display multi-husk state 2026-06-04.
- **External scratch-work space (`ext`):** A way to fling a loose, unpinned window onto the external monitor *without* pushing any of your labeled spaces over. `ext` is a **special, on-demand label** — NOT one of the canonical 10 (it's absent from `YABAI_LABELS`, so it's never auto-created/healed and never clutters the laptop). It is born the first time you fling a window and lives only on the external.
  - **`hyper+fn+g`** — fling the focused window to `ext` (created on first use) and follow. Pinned-home apps (WezTerm/Messages/etc.) and Arc-on-main/school are guarded out — exactly the "unpinned window" scope. Multiple flung windows **stack** on `ext`; cycle them with `hyper+z/x`. **`hyper+g`** focuses `ext` (no-op if it doesn't exist).
  - **Disconnect-safe placement:** `ext` is always created at a **non-first** position on the external (the external's first space is the reserved scratch). To dodge yabai's `display --focus`-then-`--create` race (a create that lands on the wrong display silently mislabels the scratch), `ensure_ext` creates the space wherever it lands, then **moves it onto the external by its stable id** and waits for the move to settle before labeling.
  - **Coming home = dissolve into `main` + delete `ext`** (it never lives on the laptop). Three triggers, identical result — every window on `ext` is moved to `main` (reachable with `hyper+1`) and the `ext` space is destroyed: **`hyper+0`** home-all (after pulling `main` home), **`hyper+\` while focused on `ext`** (overloads push — on a normal space `hyper+\` still moves the space), and **undock** (`yabai_displays.sh removed` — if macOS reparents `ext` to the laptop on disconnect it's dissolved; if macOS destroyed it outright, no-op). If `ext` is ever its display's last space (can't be destroyed), the label is dropped instead so no empty `ext` husk lingers. All in `yabai_common.sh` (`yabai_ensure_ext` / `yabai_dissolve_ext`). *(Verified live 2026-06-04: fling places on `ext`; home-all, push-on-ext, AND a real undock all dissolve to `main` and delete `ext`. The undock test had labeled spaces pushed over + a flung window on `ext`: every label came home in canonical order, pinned apps re-pinned, the flung window landed on `main`, `ext` was deleted, no stray spaces — macOS reparented `ext` to the laptop and the handler's dissolve caught it.)*
- **Arc window pinning (Hammerspoon, `dot_hammerspoon/init.lua`):** The two main Arc browser windows are pinned one-to-`main`, one-to-`school` (title-independent — it doesn't matter which goes where). Everything else about Arc, **including Little Arc popups, is left fully MANAGED** (tiled, in the stack, cyclable with `hyper+z/x`) — Little Arc is *not* floated and *not* pinned; it just lives wherever it opens.
  - The hard part: a **Little Arc** popup is byte-identical to a main window in *every* yabai field (subrole, floating, even title is the page title), so yabai cannot tell them apart. The only reliable discriminator is `AXIdentifier` (`bigBrowserWindow-*` vs `littleBrowserWindow-*`), which **yabai cannot read but Hammerspoon can**. macOS AX only exposes *current-Space* windows, so Hammerspoon reads the identifier off whatever Arc windows are on-screen and remembers which window ids are "main" (`mainSet`), accumulated as Spaces are visited.
  - It then pins **only** the remembered main ids to `main`/`school` via yabai (which *can* report any window's Space by id, cross-Space): **stably** (never disturbs a correctly-placed window; only fills an empty target; recovers drift) and **fullscreen-safe** (a fullscreen Arc window — e.g. video — is left alone, reachable via `hyper+3-9`, and returns to its space on exit). Because only `mainSet` ids are ever moved, Little Arc (never in the set) is never touched.
  - **Trigger (consistent with the other pinned apps):** yabai calls `hs -c "arcSync()"` from the **same signals that re-apply the other apps' `space=` rules** — `application_launched` (every app launch, right after `rule --apply`) and Arc `window_created` — so the Arc main windows "snap" to their spaces on the exact same cadence as Todoist/Messages/etc. **No polling/timer** (Hammerspoon's own window/space events proved unreliable with yabai's switching, so the reliable yabai signals drive it). A one-shot pass also runs on Hammerspoon load. `arcSync` self-prunes closed windows. Consequence: like the other rule-pinned apps, a manually-moved main window snaps back on the next app launch / window creation rather than instantly.
  - **Performance:** classification reads Arc's windows straight off the **application's AX element** (`hs.axuielement.applicationElement(arc):attributeValue("AXWindows")`), NOT `hs.window.allWindows()` — the latter enumerates every app's windows via AX and measured **~1.9 s** on this machine (the old 2 s timer therefore ran at ~91% of a core continuously, which is why it was removed). The app-element path makes `arcSync` ~85 ms.
  - **Force-move guard:** `yabai_send_window.sh` (`hyper+fn+<space>`) protects an Arc window sitting on `main`/`school` from being force-moved off it — the same home-space guard the other pinned apps get. It's a pure-yabai space check (no AXIdentifier), so it's reliable; the trade-off is it also shields a Little Arc that happens to be on main/school (rare). A main-only guard would need Hammerspoon's `arcFocusedKind` (kept in `init.lua`), but the AX "focused window" races with yabai's focus, so the simple space check is preferred.
  - **Dependency:** Hammerspoon must be running (set to launch at login via `hs.autoLaunch(true)`). If it's not running, the two main windows simply won't auto-pin and Little Arc behaves like any managed window — graceful degradation, not breakage. This replaced the old fragile title-based pinning in `yabai_workspace_refresh.sh`.
- **Chezmoi for everything:** All configs are templated sources in chezmoi; never edit deployed files directly. Always edit source, preview, apply, reload, commit.

## Testing status

### ✅ Verified live on dual-display hardware (2026-06-04)

A real dock/undock session exercised the cross-display paths. All passed:

- **Dock** (`display_added`) — **non-destructive**: all 10 labels stayed home on the laptop; the external came up with a single empty space (scratch), ready for manual `hyper+\` push. Cache updated to `DISPLAY_COUNT=2` / `EXTERNAL_DISPLAY_INDEX` correctly.
- **Undock** (`display_removed`) — pull-home safety net worked: every label back on the laptop in canonical order, cache reset to `DISPLAY_COUNT=1`, WezTerm intact on `terminal`.
- `hyper+\` **push** + follow — the focused space moved to the other display and **focus followed across displays** (the focus-by-id retry loop landed correctly). Pinned-app windows travel with their pushed space (verified: Notion Calendar rode its `calendar` space to the external).
- `hyper+0` **home-all** — pulled every label home and re-pinned all apps (`rule --apply`). Now resolves master from **live topology** (UUID-first).
- `yabai_reorder_spaces.sh` external scratch — reserves the external's **first space as scratch** (`pos=lo+1`); labels order from the second space. The **retry convergence fix** was verified by scrambling the external (including parking a label on the scratch slot) → **one** `reorder` call fully normalized it.
- `f13`/`f14` display focus → **mouse-follow warp** (cursor jumps to the focused display, both directions). *(The external screen-flash, also verified at the time, was subsequently disabled by user request — see the Mouse Follow section.)*
- `yabai_terminal_follow.sh` — the `terminal` label **follows WezTerm onto the external** (verified by pushing the terminal space to display 2). The **per-display husk-sweep fix** (`group_by(.display)`) was verified on a real 2-display multi-husk state: it keeps exactly one empty pad **per display** and destroys the rest high-index-first.
- **Cross-display `yabai -m space --focus <idx>`** — *previously flagged as "the single thing that can't be verified without hardware."* **Now verified**: it reliably lands focus on the external. The defensive `display --focus` fallback in `yabai_fullscreen_focus.sh` is therefore not needed (kept as belt-and-suspenders if ever wanted).
- **`yabai_send_window.sh` cross-display follow** (the verify-by-label + `space --focus` fallback) — verified **both directions** with a movable non-pinned window (Preview): laptop→external and external→laptop, focus follows the window each time.
- **Shared `yabai_common.sh` helper (DRY refactor)** — re-verified on dock that the consolidated `yabai_master_index()` / `yabai_load_cache()` behave identically: dock cache-write (`DC=2/MASTER=1/EXTERNAL=2`), push, external scratch + reorder convergence, and home-all + re-home all pass through the helper unchanged. (The undock removed-branch shares the same resolver + pull-home loop → verified by equivalence.)

*Caveat observed:* firing several pushes in rapid scripted succession (sub-second, with manual `--move` interleaved) can transiently strand a pinned window on the wrong space. It self-heals on the next `home-all`/`rule --apply`, and a normal human-paced single `hyper+\` carries the window correctly — so this is a stress-test artifact, not a real-use defect. *(Also: pushing the **terminal** space specifically didn't always land focus on it via the push-follow loop, but the dedicated `hyper+\`` focus-terminal binding reaches WezTerm on the external reliably.)*

### ⚠️ Still needs testing

Nothing outstanding. *(The former "screen-flash under a rapid burst" item is moot — the flash was disabled 2026-06-04. The former "WezTerm fullscreen on the external" item is removed — WezTerm is intentionally not fullscreenable; see the design note above.)*

### ✅ Verified single-display (2026-06-04)
- **WezTerm startup placement** — starts as a **normal window** (auto-fullscreen removed); lands on `terminal` at canonical index 1 via the `space=terminal` rule + `window_created` hook. Intentionally **not** fullscreenable (`hyper+fn+m` no-ops on it — see the design note).
- **Safe/idempotent paths** — focus by label, `space_move` early-exit, `display.sh external` no-op, `fullscreen_focus` (WezTerm excluded), `terminal_follow` fast-path, `reorder_spaces` no-op, `workspace_refresh` byte-identical cache across runs.

### Accessing a fullscreen app on the EXTERNAL display
`yabai_fullscreen_focus.sh` lists *all* native-fullscreen windows across *all* displays (`sort_by(.display, .space)`), so an external fullscreen app (Preview, browser) is in the `hyper+3`…`hyper+9` ordinal list (after any laptop fullscreen apps) and is reached via `yabai -m space --focus <idx>` — and that cross-display focus is **now verified** (see above), so this case is solid. (WezTerm is excluded from these ordinals and is not fullscreenable anyway — see the design note.)