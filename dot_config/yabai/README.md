# Yabai + Skhd + Karabiner-Elements + WezTerm: Complete Setup Reference

> *Docs audited & synced to config: **2026-06-06** — multi-agent review of the bsp keybind set (focus/resize/balance/split/rotate/mirror) + the event-driven self-heal. 0 critical/high correctness findings; no duplicate or BetterTouchTool-reserved (`hyper+a`/`hyper+s`) binds; pinned-window guards intact.*

## 1. Overview & Mental Model

This is a single-laptop-first tiling window manager setup optimized for seamless occasional multi-display work. The system treats the built-in MacBook display as the permanent "master" home for all work, with an optional external display as a temporary "external" work surface that comes and goes via dock.

**Core Mental Model:**

- **Master display (laptop)**: Always present, hosts all 10 canonical labeled workspaces (terminal, main, school, todo, schedule, mail, calendar, messages, chatgpt, codex). Stable reference point.
- **External display**: Optional. Comes up empty-and-ready when docked; user manually pushes workspaces there (or pulls them back home). Automatically healed on undock.
- **Labeled spaces, not indexed**: Spaces are identified by stable **labels** (not fragile array indices), because macOS renumbers indices whenever Mission Control is touched or displays change. All bindings, rules, and scripts use labels as the canonical reference.
- **Stack layout**: Only one window visible at a time per space; other windows are hidden in a z-order stack. Navigate with `hyper+z` (next) / `hyper+x` (prev).
- **Hyper modifier**: Caps Lock remapped (via Karabiner) to Cmd+Ctrl+Opt+Shift; this unified modifier powers nearly all window-manager shortcuts.

## 2. Where Everything Lives

### File Map: Configuration Sources & Targets

| Source (Chezmoi) | Target (Deployed To) | Type | Purpose |
|---|---|---|---|
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/yabai/executable_yabairc` | `~/.config/yabai/yabairc` | Executable shell script | Window manager core: signals, rules, app pinning, layout |
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/skhd/skhdrc.tmpl` | `~/.config/skhd/skhdrc` | Template config | Hotkey daemon: all keybindings (hyper+key → yabai commands) |
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/wezterm/wezterm.lua.tmpl` | `~/.config/wezterm/wezterm.lua` | Template config | Terminal emulator: startup, keybindings, tmux integration |
| `/Users/mackhaymond/.local/share/chezmoi/dot_config/private_karabiner/private_karabiner.json` | `~/.config/karabiner/karabiner.json` | JSON config (git-ignored) | Key remapping: caps_lock→hyper, F-key aliases (F18/F19/F13/F14) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_workspace.sh` | `~/code/various_scripts/yabai_workspace.sh` | Executable script | Focus workspace by label |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_send_window.sh` | `~/code/various_scripts/yabai_send_window.sh` | Executable script | Move focused window to space and follow focus (respects pinned homes) |
| `…/code/various_scripts/executable_yabai_send_window_external.sh` | `~/code/various_scripts/yabai_send_window_external.sh` | Executable script | Fling the focused unpinned window to the external's on-demand `ext` scratch-work space (create + follow). See the "External scratch-work space" design note |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_workspace_refresh.sh` | `~/code/various_scripts/yabai_workspace_refresh.sh` | Executable script | Reconcile canonical labels, refresh display topology cache |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_display.sh` | `~/code/various_scripts/yabai_display.sh` | Executable script | Focus display by name (master/external) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_space_move.sh` | `~/code/various_scripts/yabai_space_move.sh` | Executable script | Push/pull spaces between displays |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_displays.sh` | `~/code/various_scripts/yabai_displays.sh` | Executable script | Hotplug handler: dock/undock logic |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_skhd_mode.sh` | `~/code/various_scripts/yabai_skhd_mode.sh` | Executable script | Toggle space layout (bsp ↔ stack) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_toggle_float.sh` | `~/code/various_scripts/yabai_toggle_float.sh` | Executable script | Toggle the focused window's float (`hyper+t`); both stack & bsp; respects pinned homes |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_skhd_stack_next.sh` | `~/code/various_scripts/yabai_skhd_stack_next.sh` | Executable script | **`hyper+z`, layout-aware:** STACK → next stack layer; BSP → mirror tree horizontally (`--mirror x-axis`) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_skhd_stack_prev.sh` | `~/code/various_scripts/yabai_skhd_stack_prev.sh` | Executable script | **`hyper+x`, layout-aware:** STACK → previous stack layer; BSP → mirror tree vertically (`--mirror y-axis`) |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_mouse_follow.sh` | `~/code/various_scripts/yabai_mouse_follow.sh` | Executable script | Warp cursor to newly focused display |
| `…/code/various_scripts/executable_yabai_heal.sh` | `~/code/various_scripts/yabai_heal.sh` | Executable script | **Debounced self-heal** — coalesce `space_destroyed` / `mission_control_exit` into one `yabai_workspace_refresh` (single-flight mkdir lock + settle) |
| `…/code/various_scripts/executable_yabai_startup_reconcile.sh` | `~/code/various_scripts/yabai_startup_reconcile.sh` | Executable script | **Login-race fix** — backgrounded at startup; re-loads the SA + polls (re-apply rules / re-pin Arc) until pinned apps reach their home spaces, so they land correctly without a manual restart. Single-flighted |
| `/Users/mackhaymond/.local/share/chezmoi/code/various_scripts/executable_yabai_screen_flash.sh` | `~/code/various_scripts/yabai_screen_flash.sh` | Executable script | **DISABLED (dormant)** — was the external-display focus border flash; the signal was removed 2026-06-04 |
| `…/code/various_scripts/yabai_screen_flash.js` | `~/code/various_scripts/yabai_screen_flash.js` | JXA helper | **DISABLED (dormant)** — drew the border overlay for `yabai_screen_flash.sh` |
| `…/code/various_scripts/executable_yabai_reorder_spaces.sh` | `~/code/various_scripts/yabai_reorder_spaces.sh` | Executable script | Keep labeled spaces in canonical order per display |
| `…/code/various_scripts/executable_yabai_fullscreen_focus.sh` | `~/code/various_scripts/yabai_fullscreen_focus.sh` | Executable script | Focus the Nth native-fullscreen app (`hyper+3-9`), WezTerm excluded |
| `…/code/various_scripts/executable_yabai_terminal_follow.sh` | `~/code/various_scripts/yabai_terminal_follow.sh` | Executable script | Keep `terminal` label on WezTerm in/out of fullscreen; sweep husk spaces |
| `…/code/various_scripts/yabai_common.sh` | `~/code/various_scripts/yabai_common.sh` | Sourced shell lib (not executable) | **Shared helper** — single source of the master-display UUID, the canonical `YABAI_LABELS` list, `yabai_master_index()` (UUID-then-area resolver), and `yabai_load_cache()`. Sourced by the workspace/display/move/refresh/reorder/displays scripts; required sibling |
| `…/dot_hammerspoon/init.lua` | `~/.hammerspoon/init.lua` | Lua config | **Hammerspoon**: classify Arc windows via AXIdentifier; pin the two main windows to main/school (Little Arc left managed). Required dependency, launches at login |
| `…/swiftbar_plugins/executable_yabai_layers.1s.sh` | `~/swiftbar_plugins/yabai_layers.1s.sh` | Executable plugin | **SwiftBar** menu-bar plugin — the **live** status indicator. Shows current/total stack layer (e.g. `2 / 3`), or `BSP`/`FLOAT`, refreshed every 1s. Read-only/passive (no window-manager effect) |
| `…/dot_config/sketchybar/{items,plugins}/yabai_layers.sh` | `~/.config/sketchybar/{items,plugins}/yabai_layers.sh` | Shell scripts | **Dead/superseded** sketchybar version of the same layer indicator. sketchybar is **not running**; SwiftBar replaced it. Kept in-tree only as a dormant alternative |
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
**Monitoring:** Activity Monitor, DaisyDisk, iStat Menus (also disables border)  
**Other:** Mail, Finder, Steam, BetterZip, Python REPL windows, DevPod, Setapp, Permute, TI-Nspire, LiveMath Maker, Antidote

#### App-to-Space Pinning Rules

These apps automatically appear on their designated space when launched:

| App | Target Space |
|-----|--------------|
| wezterm-gui, WezTerm | terminal |
| Todoist | todo |
| Granola | schedule |
| Spark Mail | mail |
| Notion Calendar | calendar |
| Messages | messages |
| ChatGPT | chatgpt |
| Codex | codex |

**Arc (browser):** *not* a yabai rule (the two main windows go to two different
spaces, and Little Arc is indistinguishable to yabai). The two main Arc windows
are pinned to `main`/`school` by **Hammerspoon** (`dot_hammerspoon/init.lua`) via
AXIdentifier; Little Arc stays managed. See the "Arc window pinning" design note.

**Why label-based queries?** When yabai queries spaces, it uses labels (e.g., `yabai -m query --spaces --space terminal`) rather than indices. This makes all rules robust to index renumbering caused by Mission Control or display hotplug.

> **Caveat — `space=` *rules* store a resolved index, not the label.** yabai resolves `space=terminal` to a numeric index at rule-add time (e.g. `space:1`) and keeps it frozen; `yabai -m rule --list` shows the number, not the label. This stays correct only while the reorder keeps the labeled space at that index — always true on a single display, and on dual-display while the label stays home. The authoritative, index-agnostic placement for WezTerm is the `window_created` nudge (which re-queries `--space terminal` live); the frozen rule is just a belt-and-suspenders re-pin for a missed `window_created` event. (The other `space=` apps share the freeze but their labels never roam, so it never bites them.)

#### Signal Handlers

**1. `dock_did_restart`** → `sudo yabai --load-sa`. Reloads the scripting addition (needed for native-fullscreen, space create/destroy, etc.) after the Dock restarts.

**2. `window_created`**
- **WezTerm:** if a *normal* WezTerm lands on the wrong space, move it to terminal. A *fullscreen* WezTerm is left alone (guarded by `is-native-fullscreen`) so it isn't yanked out of fullscreen.
- **Arc:** call `hs -c "arcSync()"` (Hammerspoon re-pins the Arc main windows; Little Arc untouched).
- **Other apps:** left wherever they land. The terminal space is **not** reserved — any window may share it with WezTerm (the old non-WezTerm "bounce" was removed).

**3. `space_changed`** → `yabai_terminal_follow.sh` then re-activate WezTerm. The follow hook keeps the `terminal` label pinned to WezTerm wherever it roams (including in/out of a native-fullscreen Space), reorders, and sweeps surplus empty husk spaces. Cheap no-op when WezTerm hasn't moved.

**4. `application_launched`** → `yabai -m rule --apply` (re-pins Todoist/Messages/etc.) **and** `hs -c "arcSync()"` (re-pins the Arc main windows) — one consistent "snap" moment.

**5. `display_added` (label `workspace_display_added`)** → `yabai_displays.sh added`: debounced hotplug; settles display count and refreshes cache (non-destructive; external comes up empty).

**6. `display_removed` (label `workspace_display_removed`)** → `yabai_displays.sh removed`: pulls all labeled spaces home to the master display.

**7. `display_changed` (label `mouse_follow_display`)** → `yabai_mouse_follow.sh`: warps cursor to the newly focused display.

**8. `space_destroyed` (label `heal_space_destroyed`)** and **`mission_control_exit` (label `heal_mission_control`)** → `yabai_heal.sh` → `yabai_workspace_refresh.sh`. Event-driven self-heal: a destroyed/merged labeled space (the classic label-drop — e.g. a fullscreen collapse merging an adjacent space) or a Mission Control exit (it renumbers/merges spaces) reconciles the canonical labels + order. `yabai_heal.sh` single-flights + settles (mkdir lock, `YABAI_HEAL_SETTLE`, default 0.4s) so a burst (a Mission Control session churning several spaces) heals exactly **once**. refresh is idempotent (~0.34s) and these events are infrequent → no idle/poll cost. **Not** hooked to `space_created` (refresh may create a space → would self-trigger) or `window_destroyed` (too noisy; window closes don't drop labels).

**9. ~~`display_changed` (label `flash_external_display`)~~** → **DISABLED** (2026-06-04, user request). The external-display border flash signal was removed from `yabairc`. The helper scripts `yabai_screen_flash.sh` / `.js` remain in `code/various_scripts` as dormant; re-enable by restoring the `YABAI_SCREEN_FLASH` env var + a `display_changed` signal calling it.

*(Also: a one-shot startup sync — `"$YABAI_WORKSPACE_REFRESH" startup` — runs near the **top** of yabairc, before the rules. Specific line numbers are intentionally omitted here — they drift; grep the signal name in `yabairc`.)*

**Startup reconciliation** (`yabai_startup_reconcile.sh`, run **backgrounded** right after the startup `rule --apply`): fixes the **login race** where pinned apps land on the wrong space. At login, macOS restores app windows around when yabai starts, so windows created before the signals registered get no `window_created`/`application_launched` event, and the one-shot `rule --apply` can run *before* those windows exist (or before `--load-sa` finishes — window→space moves need the scripting addition). The reconcile re-loads the scripting addition once (`sudo -n`), then **polls until stable** — repeatedly re-applying the `space=` rules + re-pinning Arc until every *running* pinned app is on its home space, or a hard cap (`YABAI_RECONCILE_CAP`, default ~90 s). This self-truncates on a fast login (exits in ≈0.2 s once everything's home) and self-extends for slow-launching apps (Electron: ChatGPT, Notion Calendar, Messages), so it's more robust than a fixed ramp that could miss an app finishing after the last pass. Backgrounded so it never blocks startup; single-flighted (mkdir lock, like `yabai_heal.sh`) so repeated restarts don't stack overlapping polls; idempotent. Supersedes the old workaround of manually restarting yabai after login.

> **Debugging signals:** there is no `yabai -m query --signals` in this yabai version (it errors `unknown command`). The authoritative list of registered signals is the `signal --add` lines in `yabairc` — grep them there.

#### Environment Variables Exported

| Variable | Value | Consumed By |
|----------|-------|-------------|
| `YABAI_WORKSPACE_REFRESH` | `${HOME}/code/various_scripts/yabai_workspace_refresh.sh` | Startup only (the one `"$YABAI_WORKSPACE_REFRESH" startup` call). The hotplug/reader scripts run the refresh script too, but via their own `$SCRIPT_DIR` path, not this env var. |
| `YABAI_DISPLAYS` | `${HOME}/code/various_scripts/yabai_displays.sh` | `display_added` / `display_removed` signals |
| `YABAI_MOUSE_FOLLOW` | `${HOME}/code/various_scripts/yabai_mouse_follow.sh` | `display_changed` signal |
| `YABAI_HEAL` | `${HOME}/code/various_scripts/yabai_heal.sh` | `space_destroyed` / `mission_control_exit` signals (debounced self-heal) |
| `YABAI_STARTUP_RECONCILE` | `${HOME}/code/various_scripts/yabai_startup_reconcile.sh` | Backgrounded once at yabai startup (login-race fix: polls — re-apply rules + Arc re-pin — until pinned apps are home, capped by `YABAI_RECONCILE_CAP` ≈90 s) |

### 3.2 Skhd Hotkey Daemon (skhdrc)

**File:** `~/.config/skhd/skhdrc`

Skhd binds keyboard events (from Karabiner) to yabai commands. All paths are templated during chezmoi apply; `{{ .chezmoi.homeDir }}` becomes `/Users/mackhaymond`.

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

Move focus to a labeled space without moving it. Focus stays on the current display.

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
| `hyper - f` | F | `yabai_workspace.sh focus chatgpt` | chatgpt |
| `f18` | Caps+Esc | `yabai_workspace.sh focus codex` | codex |

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
| `hyper + fn - f` | Fn+F | `yabai_send_window.sh chatgpt` | chatgpt |
| `f19` | Fn+Caps+Esc | `yabai_send_window.sh codex` | codex |

**Pinned Apps (cannot be sent off their home spaces):**
- wezterm → terminal
- Todoist → todo
- Granola → schedule
- Spark Mail → mail
- Notion Calendar → calendar
- Messages → messages
- ChatGPT → chatgpt
- Codex → codex
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

Bare-hyper bsp cluster: directional focus, resize, balance, split-orientation, and rotate. All are inline `yabai` commands (no script). No-ops / harmless in a stack space. Note `hyper - a` and `hyper - s` are **reserved by BetterTouchTool** — no skhd bind may use them.

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
| `hyper - t` | T | `yabai_toggle_float.sh` | Toggle the focused window's float (works in **both** stack & bsp). Refuses on a pinned app on its home space + Arc on main/school; manage=off apps are not guarded |
| `hyper + fn - m` | Fn+M | `yabai -m window --toggle native-fullscreen` | Toggle native fullscreen (global; **no-op on WezTerm** by design — see the "WezTerm is not fullscreenable" note) |
| `hyper + fn - b` | Fn+B | `yabai_skhd_mode.sh` | Toggle space layout (bsp ↔ stack) |

#### Native-Fullscreen App Access (Hyper)

Reach apps put into macOS native fullscreen — they live in their own Spaces outside the labeled model, so the focus-workspace keys can't reach them. Ordinal = mission-control order (display, then space index); **WezTerm is excluded** (it's the terminal, reached with `hyper+\``).

| Keybinding | Key | Script | Action |
|---|---|---|---|
| `hyper - 3` … `hyper - 9` | 3–9 | `yabai_fullscreen_focus.sh 1…7` | Focus the 1st…7th native-fullscreen app (no-op if that many aren't open) |

*(`hyper - 1`/`- 2` = focus main/school; `hyper - 0` = pull-home. So 3–9 were free.)*

### 3.3 Karabiner-Elements Key Remapping

**File:** `~/.config/karabiner/karabiner.json`

Karabiner preprocesses keyboard input at the OS level, creating the hyper modifier and translating raw key presses to F-keys that skhd can bind.

#### Core Remapping: Caps Lock → Hyper

**From:** `caps_lock` (any modifiers optional)  
**To:** `left_shift + left_command + left_control + left_option` (the "hyper" modifier)

This single remapping enables nearly every downstream binding.

#### Complex Modifications: F-Key Chords

| From | To | Skhd Binding | Purpose |
|------|----|----|---------|
| F1 + hyper | F13 | `f13 : yabai_display.sh master` | Focus master (laptop) display |
| F2 + hyper | F14 | `f14 : yabai_display.sh external` | Focus external display |
| Escape + hyper (no fn) | F18 | `f18 : yabai_workspace.sh focus codex` | Focus codex workspace |
| Escape + hyper + fn | F19 | `f19 : yabai_send_window.sh codex` | Send window to codex workspace |
| Caps_Lock + Escape (simultaneous) | F18 | `f18 : yabai_workspace.sh focus codex` | Alt ergonomic path to focus codex |
| Fn + Caps_Lock + Escape (simultaneous) | F19 | `f19 : yabai_send_window.sh codex` | Alt ergonomic path to send to codex |

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
| `yabai_workspace_refresh.sh` | (none; on-demand) | Reconcile canonical labels on all displays; refresh display topology cache |
| `yabai_heal.sh` | (none; signal handler) | Debounced self-heal — single-flight (mkdir lock) + settle, then `yabai_workspace_refresh.sh`. Bound to `space_destroyed` / `mission_control_exit` |
| `yabai_startup_reconcile.sh` | (none; backgrounded at startup) | Login-race fix — re-loads the SA (`sudo -n`) + **polls until stable** (re-apply rules + Arc re-pin until every running pinned app is home, ~90 s cap) so restored windows reach their pinned spaces without a manual yabai restart. Single-flighted (mkdir lock) |
| `yabai_skhd_mode.sh` | (none) | Toggle focused space layout (bsp ↔ stack) |
| `yabai_toggle_float.sh` | (none) | Toggle the focused window's float (`hyper+t`); works in both stack & bsp; refuses on a pinned app on its home space + Arc on main/school (manage=off apps not guarded) |
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
6. User manually pushes workspaces via `hyper+\` (skhd → yabai_space_move.sh push) or pulls master workspaces to external display.

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

The 10 spaces are always maintained in the order: terminal, main, school, todo, schedule, mail, calendar, messages, chatgpt, codex.

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

The single most fragile dependency in the whole system is the **scripting addition**, which yabai needs for native-fullscreen, space create/destroy, and the husk sweep. yabairc loads it on every (re)start via `sudo yabai --load-sa` (line ~3) and re-loads it from the `dock_did_restart` signal. For that `sudo` to run **non-interactively from a config/signal with no TTY**, three things must be in place — all currently satisfied on this machine, but required to reproduce on a new one:

1. **Partially-disabled SIP.** `csrutil status` must show a *Custom Configuration* with at least **Filesystem Protections: disabled** (set from Recovery with `csrutil enable --without fs` or equivalent). Full SIP blocks the scripting addition.
2. **Scripting addition installed.** `sudo yabai --install-sa` puts `yabai.osax` under `/Library/ScriptingAdditions/`. Re-run after a yabai upgrade (the binary hash changes — see #3).
3. **Passwordless sudoers entry**, hash-pinned to the yabai binary, at `/etc/sudoers.d/yabai` (mode `0440`, owned by root):
   ```
   <admin-user> ALL = (root) NOPASSWD: sha256:<hash-of-yabai-binary> /opt/homebrew/bin/yabai --load-sa
   ```
   Generate the current line with `yabai --check-sa` (newer yabai) or copy the hash yabai prints. **A yabai version bump changes the binary hash and silently invalidates this line** — `--load-sa` then fails quietly and SA-dependent features stop working with no error in the config.

**Symptom of a broken SA layer:** yabai starts and tiling/focus all work, but native-fullscreen toggling, space create/destroy, or the WezTerm husk sweep silently no-op. Fix = re-install the SA and regenerate the sudoers hash, not the config.

Other login-time dependencies: **Karabiner** (caps_lock→hyper, the F13/F14/F18/F19 chords) and **Hammerspoon** (`hs.autoLaunch(true)`, for Arc pinning — degrades gracefully if absent). `skhd` and `yabai` run as user LaunchAgents.

### Focus a Workspace

**Goal:** Switch focus to a labeled workspace without moving it.

**Action:**
```bash
# From anywhere, press hyper+<key> for the workspace
hyper - 1              # Focus "main" workspace
hyper - 0x32 (`)       # Focus "terminal" workspace
f18                    # Focus "codex" workspace
```

**What happens:**
1. skhd captures the keybinding.
2. skhd invokes `yabai_workspace.sh focus <label>`.
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
f19                    # Send to "codex" and follow
```

**What happens:**
1. skhd captures the keybinding.
2. skhd invokes `yabai_send_window.sh <label>`.
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
1. skhd captures the keybinding.
2. skhd invokes `yabai_space_move.sh push`.
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
1. skhd invokes `yabai_space_move.sh home-all`.
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
# Press F14 (hyper+F2, remapped by Karabiner)
f14                    # Focus external display
```

**What happens:**
1. skhd invokes `yabai_display.sh external`.
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
nano /Users/mackhaymond/.local/share/chezmoi/dot_config/private_karabiner/private_karabiner.json
# Be careful with JSON syntax!
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

**For skhd:**
```bash
skhd --reload
```

**For yabai (if changes affect signal handlers, rules, or layout):**
```bash
# Soft restart (keeps windows, reloads config)
yabai --restart-service

# Or hard restart (if soft fails)
launchctl kickstart -k gui/$(id -u)/com.koekeishiya.yabai
```

> On restart, yabairc re-runs `sudo yabai --load-sa` (the scripting addition). This depends on the passwordless-sudo entry described in **Prerequisites & Bootstrap**. If a restart *appears* to succeed but scripting-addition features (native fullscreen, space create/destroy, the husk sweep) quietly stop working, check the scripting addition and the sudoers entry — not the config diff.

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
| `hyper - f` | F | Focus chatgpt | |
| `f18` | Caps Lock+Escape | Focus codex | Karabiner-mapped |
| **Send Window to Workspace** |
| `hyper + fn - 0x32` | Fn+Backtick | Send to terminal | Respects pinned homes |
| `hyper + fn - 1` | Fn+1 | Send to main | Respects pinned homes |
| `hyper + fn - 2` | Fn+2 | Send to school | Respects pinned homes |
| `hyper + fn - tab` | Fn+Tab | Send to todo | Respects pinned homes |
| `hyper + fn - q` | Fn+Q | Send to schedule | Respects pinned homes |
| `hyper + fn - w` | Fn+W | Send to mail | Respects pinned homes |
| `hyper + fn - e` | Fn+E | Send to calendar | Respects pinned homes |
| `hyper + fn - d` | Fn+D | Send to messages | Respects pinned homes |
| `hyper + fn - f` | Fn+F | Send to chatgpt | Respects pinned homes |
| `f19` | Fn+Caps Lock+Escape | Send to codex | Karabiner-mapped |
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
| `hyper - t` | T | Toggle window float | Both stack & bsp; `yabai_toggle_float.sh` — refuses on pinned-on-home + Arc on main/school |
| `hyper + fn - m` | Fn+M | Toggle native fullscreen | Global; no-op on WezTerm by design |
| `hyper + fn - b` | Fn+B | Toggle space layout (bsp ↔ stack) | |
| `hyper - a` / `hyper - s` | A / S | *(reserved by BetterTouchTool)* | Not skhd binds — never assign |
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
| `hyper + fn - 0x2C` | Fn+/ (`?`) | Toggle the on-screen keybind help overlay | Hammerspoon `yabaiHelpToggle` (`init.lua`); on the fn layer because bare `hyper+/` is swallowed by macOS's reserved `cmd+?` |
| `esc` | Escape | Close the help overlay | Active only while the overlay is showing |

## Notes & Key Design Decisions

- **Single-laptop-first:** All 10 canonical workspaces live on the master (laptop) display by default. External display is optional and transient.
- **Label-based stability:** Spaces are identified by labels, not indices, surviving Mission Control renumbering and display hotplug.
- **Non-destructive dock:** External monitor comes up empty; user manually pushes workspaces.
- **Pull-home on undock:** Automatic safety net prevents orphaned windows on non-existent displays.
- **Terminal space (not reserved):** WezTerm's home space, but other windows may land on it and stay — the old non-WezTerm bounce was removed. WezTerm itself is still nudged onto it on launch.
- **Pinned apps:** Certain apps (Todoist, Granola, Spark Mail, etc.) are sticky and cannot be moved off their home spaces.
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